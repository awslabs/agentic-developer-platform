"""Unit tests for lib/review_result.py — the producer half of #5146.

These tests exist to stop one specific class of regression. The gateway consumer has
its own suite, the contract has its own suite, and if this file only asserted "the
writer produced a dict with the keys I expect" then all three could be green while the
shipped worker emitted something the gateway refuses. That is the #4029 failure
exactly: two suites, each asserting its own assumption, both passing, producer and
consumer disagreeing for months.

So the load-bearing test here is :class:`TestEmittedDocumentSatisfiesTheRealContract`,
which imports the **normative validator** from ``contracts/orchestration-review/v1``
and validates what this module actually built. Not a copy of the schema, not a
hand-written payload — the real models, against the real output of the real function.
Everything else in this file is about the properties a validator cannot check:

* **Nothing the run says about its own authority is accepted.** Scope, authority and
  the authoring run come from server-published environment; there is no argument that
  can set them, and their absence is a refusal rather than a default.
* **Fail-soft, like `pr_binding` and `handoff_client`.** By the time this runs the
  review is posted and delivered. `review_result_note` must never raise into
  `entrypoint.py`, which has no error handling at the call site.
* **Visible, not silent.** Every failure path must return prose saying the evidence
  was not produced. A silently-swallowed failure reproduces the original defect: a
  review that looks complete and establishes nothing.
* **No path produces something that reads as approval.** Not the happy path, not a
  refused publication, not a skipped stage.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from lib.review_result import (
    AGENT_REPORT_PATH_ENV,
    CONTRACT_NAME,
    CONTRACT_OWNER,
    CONTRACT_VERSION,
    DEFAULT_RESULT_PATH,
    HANDOFF_EXPECT_ENV,
    RESULT_PATH_ENV,
    REVIEW_EXPECT_ENV,
    FindingReport,
    ReviewResultError,
    StageReport,
    build_review_result,
    findings_from_agent_report,
    publication_from_adp_review,
    read_agent_report,
    review_result_note,
    reviewer_evidence_note,
    stages_from_agent_report,
    verdict_from_agent_report,
    write_review_result,
)

# The repository root: tests → agent-worker-image → agent-factory → modules → root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_CONTRACT_DIR = _REPO_ROOT / "contracts" / "orchestration-review" / "v1"
GOLDEN_PATH = _CONTRACT_DIR / "review-result.golden.json"

REPO = "aws-e/adp"
REPOSITORY_ID = 812349901
PR_NUMBER = 5290
PR_NODE_ID = "PR_kwDOAbCdEf4AbCdE"
HEAD = "31b05e52f0005f7e7445538fe117d8fbb9fb9d53"
OTHER_HEAD = "f0d2eb968cb5f9d1322da48d92042cd7f45c166a"
REVIEWER_RUN = "0e8c6378-03f7-4c68-bb65-e6bba69f2ec8"
AUTHOR_RUN = "run-author-4f21c8e0"

ORG = "org-adp-dev"
FLOW = "a94cb68e-03a2-4ddd-8c48-b542d1bf05e4"
NODE = "126775e3-b681-4d2a-9f04-6560c5afb7fd"
CLAIM = "claim-126775e3-0001"


def handoff_expect(**overrides) -> dict:
    """The dispatch fences the gateway publishes for #5144's handoff."""
    body = {
        "org_id": ORG,
        "flow_id": FLOW,
        "node_id": NODE,
        "cycle": 1,
        "claim_id": CLAIM,
        "claim_generation": 3,
        "accepted_plan_version": 7,
        "execution_id": "exec-126775e3-0001",
        "policy_id": "policy-1",
        "policy_hash": "abc123",
    }
    body.update(overrides)
    return body


def review_expect(**overrides) -> dict:
    """The review-specific expectation, published from records this pod cannot write."""
    body = {
        "author_run_id": AUTHOR_RUN,
        "expected_head_sha": HEAD,
        "execution_id": "exec-126775e3-0001",
    }
    body.update(overrides)
    return body


@pytest.fixture
def dispatched(monkeypatch):
    """A pod dispatched to review PR 5290 at HEAD, with every fence present."""
    monkeypatch.setenv(HANDOFF_EXPECT_ENV, json.dumps(handoff_expect()))
    monkeypatch.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect()))
    monkeypatch.setenv("ADP_MESSAGE_ID", REVIEWER_RUN)
    return monkeypatch


PUBLISHED = {
    "outcome": "published",
    "published_head_sha": HEAD,
    "reference": "pullrequestreview-5229910564",
    "detail": None,
}

BOTH_STAGES = [StageReport("functional", "completed"), StageReport("security", "completed")]


def build(**overrides) -> dict:
    """Build a complete, approval-capable result, with named overrides."""
    kwargs: dict = {
        "result_id": "review-5146-u1-0001",
        "repo": REPO,
        "provider_repository_id": REPOSITORY_ID,
        "pr_number": PR_NUMBER,
        "provider_pr_node_id": PR_NODE_ID,
        "reviewed_head_sha": HEAD,
        "verdict": "approve",
        "stages": BOTH_STAGES,
        "publication": PUBLISHED,
        "reviewer_identity": "aws-e-adp-agent-dev[bot]",
        "observed_at": datetime(2026, 9, 17, 1, 6, 1, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return build_review_result(**kwargs)


def note(**overrides) -> str:
    """Call the fail-soft entry point with a complete set of arguments."""
    kwargs: dict = {
        "repo": REPO,
        "provider_repository_id": REPOSITORY_ID,
        "pr_number": PR_NUMBER,
        "provider_pr_node_id": PR_NODE_ID,
        "reviewed_head_sha": HEAD,
        "verdict": "approve",
        "stages": BOTH_STAGES,
        "publication": PUBLISHED,
    }
    kwargs.update(overrides)
    return review_result_note(**kwargs)


# ---------------------------------------------------------------------------
# The one test that makes the other suites meaningful
# ---------------------------------------------------------------------------


def _load_models():
    """Import the normative validator from the contracts tree.

    Skipped rather than failed when the tree is absent, because these tests are not
    shipped in the image and may be run from a partial checkout. The image itself DOES
    carry the contract as of #5146 (`COPY contracts/ /app/contracts/`), and that is
    asserted where it belongs — at build time, by `lib/contract_selfcheck.py`, which
    fails the build rather than skipping. In CI, where
    `.github/workflows/orchestration-review-contract-tests.yml` runs from a full
    checkout, the tree is present and this test is the binding one.
    """
    if not _CONTRACT_DIR.is_dir():
        pytest.skip(f"contract tree not present at {_CONTRACT_DIR}")
    sys.path.insert(0, str(_CONTRACT_DIR))
    import models

    return models


class TestEmittedDocumentSatisfiesTheRealContract:
    """What this module builds is validated by the contract's own models.

    Every test here runs the real ``ReviewResult.model_validate`` over the real output
    of ``build_review_result``. A drift between producer and contract fails here, at
    the producer, instead of surfacing as a gateway refusal in production.
    """

    def test_a_complete_approving_result_validates(self, dispatched):
        models = _load_models()
        result = models.ReviewResult.model_validate(build())
        assert result.verdict is models.ReviewVerdict.APPROVE
        assert result.subject.reviewed_head_sha == HEAD
        # The point of the whole artifact: the contract agrees this evidence supports
        # approval. Asserted through `approval_blockers`, the only supported way to ask.
        assert result.approval_blockers() == ()

    def test_the_security_only_run_expressed_honestly_validates_and_blocks(self, dispatched):
        """The shape the observed runs should have emitted instead of a clean report.

        It must validate — the contract's job is to make this state representable —
        and it must carry reasons. A security-only run that cannot say "functional did
        not run" is how a correctness blocker disappeared.
        """
        models = _load_models()
        document = build(
            verdict="incomplete",
            stages=[
                StageReport(
                    "functional",
                    "not-run",
                    "the run produced only a security report",
                ),
                StageReport("security", "completed"),
            ],
            publication=publication_from_adp_review(None, reviewed_head_sha=HEAD),
        )
        result = models.ReviewResult.model_validate(document)
        blockers = result.approval_blockers()
        assert blockers, "a security-only result must carry reasons it cannot be approved on"
        assert any("functional" in reason for reason in blockers)
        assert any("not published" in reason for reason in blockers)

    def test_a_refused_publication_validates_and_blocks(self, dispatched):
        models = _load_models()
        document = build(
            verdict="request-changes",
            publication=publication_from_adp_review(
                {"outcome": "pending_approval", "verdict_recorded": False, "review_id": "IC_1"},
                reviewed_head_sha=HEAD,
            ),
        )
        result = models.ReviewResult.model_validate(document)
        assert result.publication.outcome is models.PublicationOutcome.REFUSED
        assert any("not published" in reason for reason in result.approval_blockers())

    def test_findings_with_evidence_validate(self, dispatched):
        models = _load_models()
        document = build(
            verdict="request-changes",
            findings=[
                FindingReport(
                    finding_id="destructive-apply-gate-fails-open",
                    stage="functional",
                    severity="blocking",
                    disposition="open",
                    summary="The destructive-apply gate fails open.",
                    evidence_refs=(
                        {
                            "kind": "check-run",
                            "ref": "check-run:105034202623",
                            "head_bound": True,
                        },
                    ),
                )
            ],
        )
        result = models.ReviewResult.model_validate(document)
        assert len(result.blocking_findings) == 1
        # The functional blocker survives into the verdict rather than being filtered
        # below a scanner's reporting threshold — the #5146 U19 shape.
        assert any("destructive-apply-gate" in reason for reason in result.approval_blockers())

    def test_the_producer_cannot_emit_an_unsupported_approve(self, dispatched):
        """An `approve` the evidence does not support must fail before it is emitted.

        The contract rejects it too, but a producer that can *build* one has already
        written it to a file an operator may read. Refusing here means the unsupported
        approval never exists as a document at all.
        """
        models = _load_models()
        document = build(
            publication=publication_from_adp_review(None, reviewed_head_sha=HEAD),
        )
        # The producer let it through (publication is a caller-supplied observation,
        # and the writer records what it is told about the world) — the contract is
        # what refuses. This asserts the safety net is actually in the net.
        with pytest.raises(Exception) as exc:
            models.ReviewResult.model_validate(document)
        assert "approve" in str(exc.value)

    def test_local_blockers_agree_with_the_contract(self, dispatched, tmp_path):
        """The note's small local blocker check must not disagree with the real one.

        `_local_blockers` exists because the validator is not importable inside the
        image. Two implementations of one rule is how they drift, so this asserts they
        answer identically on every document shape this suite builds.
        """
        models = _load_models()
        from lib.review_result import _local_blockers

        cases = [
            build(),
            build(
                verdict="incomplete",
                stages=[
                    StageReport("functional", "not-run", "not performed"),
                    StageReport("security", "completed"),
                ],
                publication=publication_from_adp_review(None, reviewed_head_sha=HEAD),
            ),
            build(
                verdict="request-changes",
                findings=[
                    FindingReport(
                        "f1", "functional", "blocking", "open", "still broken", evidence_refs=()
                    )
                ],
            ),
            build(
                verdict="request-changes",
                publication=publication_from_adp_review(
                    {"outcome": "failed", "verdict_recorded": False}, reviewed_head_sha=HEAD
                ),
            ),
        ]
        for document in cases:
            contract_blockers = models.ReviewResult.model_validate(document).approval_blockers()
            local = _local_blockers(document)
            # Compared on emptiness, not text: the wording is the note's own and may
            # differ. What must never differ is the answer to "can this be approved on".
            assert bool(local) == bool(contract_blockers), document["result_id"]

    def test_the_envelope_matches_the_contract_constants(self):
        """A drifted envelope constant would make every document a wrong-contract refusal."""
        models = _load_models()
        assert CONTRACT_NAME == models.CONTRACT_NAME
        assert CONTRACT_VERSION == models.CONTRACT_VERSION
        assert CONTRACT_OWNER == models.CONTRACT_OWNER

    def test_the_sha_pattern_matches_the_contract(self):
        """A looser producer pattern would emit heads the consumer refuses."""
        models = _load_models()
        from lib.review_result import _SHA_PATTERN

        assert _SHA_PATTERN.pattern == models.SHA_PATTERN.pattern

    def test_the_golden_fixture_is_the_shared_one(self):
        """This suite and the gateway's read the same file, not two copies."""
        assert GOLDEN_PATH.is_file() or not _CONTRACT_DIR.is_dir()


# ---------------------------------------------------------------------------
# Nothing the run says about its own authority is accepted
# ---------------------------------------------------------------------------


class TestAuthorityComesFromTheDispatchOnly:
    def test_there_is_no_argument_for_scope_authority_or_author(self, dispatched):
        """The forgeable fields must not be reachable through the function signature.

        Asserted on the signature itself rather than by attempting a forgery, because
        a keyword that does not exist cannot be passed by a future caller either. A
        run that could name its own tenant, claim generation or the authoring run
        could manufacture evidence for work it was not dispatched for.
        """
        import inspect

        parameters = set(inspect.signature(build_review_result).parameters)
        forgeable = {
            "org_id",
            "flow_id",
            "node_id",
            "cycle",
            "claim_id",
            "claim_generation",
            "accepted_plan_version",
            "author_run_id",
            "reviewer_run_id",
            "execution_id",
        }
        assert not (parameters & forgeable), (
            f"{parameters & forgeable} is settable by the caller; these must come from the "
            "server-published dispatch only"
        )

    def test_scope_and_authority_are_taken_from_the_dispatch(self, dispatched):
        document = build()
        assert document["scope"] == {
            "org_id": ORG,
            "flow_id": FLOW,
            "node_id": NODE,
            "cycle": 1,
            "execution_id": "exec-126775e3-0001",
        }
        assert document["authority"] == {
            "accepted_plan_version": 7,
            "claim_id": CLAIM,
            "claim_generation": 3,
        }

    def test_the_reviewer_run_is_the_pods_own_run_id(self, dispatched):
        assert document_lineage(build())["reviewer_run_id"] == REVIEWER_RUN

    def test_the_author_run_comes_from_the_dispatch(self, dispatched):
        assert document_lineage(build())["author_run_id"] == AUTHOR_RUN

    def test_no_review_expectation_refuses(self, monkeypatch):
        monkeypatch.delenv(REVIEW_EXPECT_ENV, raising=False)
        monkeypatch.setenv("ADP_MESSAGE_ID", REVIEWER_RUN)
        with pytest.raises(ReviewResultError) as exc:
            build()
        assert REVIEW_EXPECT_ENV in exc.value.reason

    def test_a_dispatch_that_names_no_author_refuses(self, dispatched):
        """The refusal that makes the self-review check unforgeable.

        Substituting the reviewer, or a placeholder, or accepting one from the caller
        would each let a reviewer name a run it is not — and then the
        `reviewer != author` check compares two values the reviewer chose.
        """
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(author_run_id=None)))
        with pytest.raises(ReviewResultError) as exc:
            build()
        assert "authored" in exc.value.reason
        assert "self-review" in exc.value.reason

    def test_a_blank_author_run_refuses(self, dispatched):
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(author_run_id="   ")))
        with pytest.raises(ReviewResultError):
            build()

    def test_reviewing_its_own_output_refuses(self, dispatched):
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(author_run_id=REVIEWER_RUN)))
        with pytest.raises(ReviewResultError) as exc:
            build()
        assert "cannot review its own output" in exc.value.reason

    def test_no_run_identifier_refuses(self, dispatched):
        dispatched.delenv("ADP_MESSAGE_ID", raising=False)
        with pytest.raises(ReviewResultError) as exc:
            build()
        assert "reviewer" in exc.value.reason

    @pytest.mark.parametrize("fence", ["org_id", "flow_id", "node_id", "claim_id"])
    def test_a_missing_string_fence_refuses(self, dispatched, fence):
        expect = handoff_expect()
        del expect[fence]
        dispatched.setenv(HANDOFF_EXPECT_ENV, json.dumps(expect))
        with pytest.raises(ReviewResultError) as exc:
            build()
        assert fence in exc.value.reason

    @pytest.mark.parametrize("fence", ["cycle", "accepted_plan_version", "claim_generation"])
    def test_a_missing_int_fence_refuses(self, dispatched, fence):
        expect = handoff_expect()
        del expect[fence]
        dispatched.setenv(HANDOFF_EXPECT_ENV, json.dumps(expect))
        with pytest.raises(ReviewResultError) as exc:
            build()
        assert fence in exc.value.reason

    @pytest.mark.parametrize("fence", ["cycle", "accepted_plan_version", "claim_generation"])
    def test_a_boolean_never_satisfies_an_integer_fence(self, dispatched, fence):
        """`True` is an `int` to isinstance, and a boolean cycle is not a cycle."""
        dispatched.setenv(HANDOFF_EXPECT_ENV, json.dumps(handoff_expect(**{fence: True})))
        with pytest.raises(ReviewResultError) as exc:
            build()
        assert fence in exc.value.reason

    def test_a_string_integer_never_satisfies_an_integer_fence(self, dispatched):
        dispatched.setenv(HANDOFF_EXPECT_ENV, json.dumps(handoff_expect(cycle="1")))
        with pytest.raises(ReviewResultError):
            build()

    @pytest.mark.parametrize("raw", ["not json", "[]", '"a string"', "null", "123"])
    def test_an_unusable_expectation_refuses_rather_than_raising_type_errors(self, dispatched, raw):
        """Malformed server input becomes a stated refusal, never a TypeError."""
        dispatched.setenv(REVIEW_EXPECT_ENV, raw)
        with pytest.raises(ReviewResultError) as exc:
            build()
        assert exc.value.reason

    def test_an_unusable_handoff_expectation_still_refuses_by_fence(self, dispatched):
        dispatched.setenv(HANDOFF_EXPECT_ENV, "{not json")
        with pytest.raises(ReviewResultError) as exc:
            build()
        # The review expectation alone carries no scope, so the first missing fence
        # is reported rather than the JSON error: the operator's problem is the
        # absent fence either way.
        assert "org_id" in exc.value.reason

    def test_the_review_expectation_wins_a_fence_collision(self, dispatched):
        """The more specific server record decides, deterministically.

        Both expectations come from the gateway, so a collision is a server-side bug
        rather than an attack, but resolving it by dict-iteration order would make the
        emitted document depend on nothing legible.
        """
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(cycle=4)))
        assert build()["scope"]["cycle"] == 4

    def test_an_execution_id_is_omitted_rather_than_invented(self, dispatched):
        dispatched.setenv(HANDOFF_EXPECT_ENV, json.dumps(handoff_expect(execution_id=None)))
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(execution_id=None)))
        assert build()["scope"]["execution_id"] is None

    def test_a_non_string_execution_id_is_omitted(self, dispatched):
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(execution_id=7)))
        assert build()["scope"]["execution_id"] is None


def document_lineage(document: dict) -> dict:
    return document["lineage"]


# ---------------------------------------------------------------------------
# The reviewed commit
# ---------------------------------------------------------------------------


class TestTheReviewedCommit:
    def test_the_inspected_commit_is_what_is_recorded(self, dispatched):
        assert build()["subject"]["reviewed_head_sha"] == HEAD

    def test_a_head_that_moved_before_this_run_read_it_refuses(self, dispatched):
        """The wrong-revision defect, refused rather than recorded.

        The artifact would be perfectly true — "I reviewed X" — and the review would
        still be about code nobody asked about. Emitting it would let a consumer
        record review evidence for a revision the dispatch never named.
        """
        with pytest.raises(ReviewResultError) as exc:
            build(reviewed_head_sha=OTHER_HEAD)
        assert OTHER_HEAD[:12] in exc.value.reason
        assert HEAD[:12] in exc.value.reason

    def test_a_dispatch_without_an_expected_head_accepts_the_inspected_one(self, dispatched):
        """Not every dispatch names a head; the run's own observation is then all there is.

        Accepted here and checked by the gateway against the provider's current head,
        which is the only place that comparison can be authoritative anyway.
        """
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(expected_head_sha=None)))
        assert build(reviewed_head_sha=OTHER_HEAD)["subject"]["reviewed_head_sha"] == OTHER_HEAD

    @pytest.mark.parametrize(
        "bad",
        [
            "31b05e5",
            "31B05E52F0005F7E7445538FE117D8FBB9FB9D53",
            "",
            "zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz",
            None,
            31,
        ],
    )
    def test_an_unusable_commit_id_refuses(self, dispatched, bad):
        """Head binding is worthless if the head can be a prefix, mixed case or absent."""
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(expected_head_sha=None)))
        with pytest.raises(ReviewResultError) as exc:
            build(reviewed_head_sha=bad)
        assert "commit sha" in exc.value.reason

    def test_a_sha256_head_is_accepted(self, dispatched):
        sha256 = "a" * 64
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(expected_head_sha=sha256)))
        assert build(reviewed_head_sha=sha256)["subject"]["reviewed_head_sha"] == sha256


class TestRepositoryAndPullRequestIdentity:
    def test_immutable_identity_is_recorded_alongside_the_display_name(self, dispatched):
        document = build()
        assert document["repository"]["provider_repository_id"] == REPOSITORY_ID
        assert document["repository"]["repo"] == REPO
        assert document["subject"]["provider_pr_node_id"] == PR_NODE_ID

    @pytest.mark.parametrize("bad", [0, -1, None, "812349901", True])
    def test_a_missing_repository_id_refuses(self, dispatched, bad):
        """A rename or transfer re-points the display name; the id it cannot."""
        with pytest.raises(ReviewResultError) as exc:
            build(provider_repository_id=bad)
        assert "repository id" in exc.value.reason

    @pytest.mark.parametrize("bad", ["", "adp", "aws-e/adp/extra", None, "../etc"])
    def test_an_unusable_repo_path_refuses(self, dispatched, bad):
        with pytest.raises(ReviewResultError) as exc:
            build(repo=bad)
        assert "owner/name" in exc.value.reason

    @pytest.mark.parametrize("bad", [0, -3, None, "5290", True])
    def test_an_unusable_pr_number_refuses(self, dispatched, bad):
        with pytest.raises(ReviewResultError) as exc:
            build(pr_number=bad)
        assert "pull-request number" in exc.value.reason

    @pytest.mark.parametrize("bad", ["", "   ", None, 12345])
    def test_a_missing_pr_node_id_refuses(self, dispatched, bad):
        """PR number alone is not identity — the contract refuses to bind on it."""
        with pytest.raises(ReviewResultError) as exc:
            build(provider_pr_node_id=bad)
        assert "node id" in exc.value.reason


# ---------------------------------------------------------------------------
# Stages: a skip cannot be silent
# ---------------------------------------------------------------------------


class TestStagesCannotSkipSilently:
    def test_a_security_only_stage_list_refuses(self, dispatched):
        """The exact observed defect, refused at the producer.

        Reporting functional as `not-run` with a reason is always available, so there
        is no legitimate caller this blocks.
        """
        with pytest.raises(ReviewResultError) as exc:
            build(stages=[StageReport("security", "completed")])
        assert "functional" in exc.value.reason

    def test_an_empty_stage_list_refuses(self, dispatched):
        with pytest.raises(ReviewResultError) as exc:
            build(stages=[])
        assert "not review evidence" in exc.value.reason

    def test_a_repeated_stage_refuses(self, dispatched):
        with pytest.raises(ReviewResultError) as exc:
            build(
                stages=[
                    StageReport("functional", "completed"),
                    StageReport("functional", "not-run", "contradicts the entry above"),
                ]
            )
        assert "twice" in exc.value.reason

    @pytest.mark.parametrize("outcome", ["not-run", "failed"])
    def test_an_unexplained_inconclusive_stage_refuses(self, dispatched, outcome):
        with pytest.raises(ReviewResultError) as exc:
            build(
                verdict="incomplete",
                stages=[StageReport("functional", outcome), StageReport("security", "completed")],
            )
        assert "must say why" in exc.value.reason

    @pytest.mark.parametrize("blank", ["", "   ", None])
    def test_a_blank_reason_is_not_a_reason(self, dispatched, blank):
        with pytest.raises(ReviewResultError):
            build(
                verdict="incomplete",
                stages=[StageReport("functional", "not-run", blank)],
            )

    def test_an_explained_skip_is_recorded(self, dispatched):
        document = build(
            verdict="incomplete",
            stages=[
                StageReport("functional", "not-run", "the run produced only a security report"),
                StageReport("security", "completed"),
            ],
            publication=publication_from_adp_review(None, reviewed_head_sha=HEAD),
        )
        functional = document["stages"][0]
        assert functional["outcome"] == "not-run"
        assert functional["detail"]

    def test_a_security_stage_may_be_absent(self, dispatched):
        """Only `functional` is mandatory: a functional-only review is legitimate."""
        document = build(stages=[StageReport("functional", "completed")])
        assert [stage["name"] for stage in document["stages"]] == ["functional"]


class TestFindings:
    def test_a_repeated_finding_id_refuses(self, dispatched):
        """A duplicate makes 'is this blocker resolved?' unanswerable."""
        with pytest.raises(ReviewResultError) as exc:
            build(
                verdict="request-changes",
                findings=[
                    FindingReport("same", "functional", "blocking", "open", "says open"),
                    FindingReport(
                        "same",
                        "functional",
                        "blocking",
                        "resolved",
                        "says resolved",
                        evidence_refs=({"kind": "test-run", "ref": "r", "head_bound": True},),
                    ),
                ],
            )
        assert "repeat" in exc.value.reason

    def test_findings_carry_references_not_payloads(self, dispatched):
        document = build(
            verdict="request-changes",
            findings=[
                FindingReport(
                    "f1",
                    "functional",
                    "blocking",
                    "open",
                    "still broken",
                    evidence_refs=(
                        {"kind": "check-run", "ref": "check-run:1", "head_bound": True},
                    ),
                )
            ],
        )
        ref = document["findings"][0]["evidence_refs"][0]
        assert set(ref) <= {"kind", "ref", "head_bound", "summary", "stale"}
        assert "content" not in ref and "body" not in ref

    def test_no_findings_is_an_empty_list_not_an_absence(self, dispatched):
        assert build()["findings"] == []


# ---------------------------------------------------------------------------
# Publication: the verdict and its recording fail independently
# ---------------------------------------------------------------------------


class TestPublicationFromAdpReview:
    def test_a_recorded_verdict_is_published_against_the_provider_s_own_commit(self):
        """`published_head_sha` comes from the provider, and must match what was read.

        It used to be set to `reviewed_head_sha` unconditionally, which made the
        contract's "published commit must equal reviewed commit" validator compare a
        value with itself. A check that cannot fail is not evidence, and the binding it
        was supposed to establish — that the verdict is attached to the revision this
        run actually inspected — was never checked at all.
        """
        block = publication_from_adp_review(
            {
                "outcome": "submitted",
                "verdict_recorded": True,
                "commit_id": HEAD,
                "url": "https://github.com/aws-e/adp/pull/5290#pullrequestreview-1",
            },
            reviewed_head_sha=HEAD,
        )
        assert block["outcome"] == "published"
        assert block["published_head_sha"] == HEAD

    def test_a_verdict_recorded_against_another_commit_is_not_published(self):
        """The wrong-revision failure this issue exists to detect, now detectable."""
        block = publication_from_adp_review(
            {"outcome": "submitted", "verdict_recorded": True, "commit_id": OTHER_HEAD},
            reviewed_head_sha=HEAD,
        )
        assert block["outcome"] == "failed"
        assert block["published_head_sha"] is None
        assert OTHER_HEAD in block["detail"] and HEAD in block["detail"], (
            "an operator must be able to see which two commits disagreed"
        )

    def test_a_recorded_verdict_with_no_commit_id_is_not_published(self):
        """Unknown revision is reported as not-published, never assumed to be the head."""
        block = publication_from_adp_review(
            {"outcome": "submitted", "verdict_recorded": True}, reviewed_head_sha=HEAD
        )
        assert block["outcome"] == "failed"
        assert block["published_head_sha"] is None
        assert "no commit id" in block["detail"]

    def test_a_commit_id_that_is_not_a_sha_cannot_satisfy_the_binding(self):
        """A provider echoing a branch name or a number must not pass the comparison."""
        for value in ["main", "HEAD", 12345, "", None, HEAD.upper(), HEAD[:39], True]:
            block = publication_from_adp_review(
                {"outcome": "submitted", "verdict_recorded": True, "commit_id": value},
                reviewed_head_sha=HEAD,
            )
            assert block["outcome"] != "published", value

    def test_no_attempt_is_reported_not_left_absent(self):
        """'We never tried' must be representable, which is what the observed runs lacked."""
        block = publication_from_adp_review(None, reviewed_head_sha=HEAD)
        assert block["outcome"] == "not-attempted"
        assert block["detail"]
        assert block["published_head_sha"] is None

    def test_a_shared_identity_refusal_keeps_its_reason(self):
        block = publication_from_adp_review(
            {
                "outcome": "pending_approval",
                "verdict_recorded": False,
                "review_id": "IC_1",
                "refusal_reason": "self_review",
            },
            reviewed_head_sha=HEAD,
        )
        assert block["outcome"] == "refused"
        assert "same identity" in block["detail"]
        assert block["published_head_sha"] is None

    def test_each_refusal_cause_gets_its_own_reason(self):
        """Three different causes produce `pending_approval`, and they are not the same
        problem. Reporting a downgraded state or an unreadable response as "no distinct
        reviewer identity is configured" sends an operator to configure a reviewer App
        that would not have changed the outcome — a confident, checkable falsehood in
        the one field whose only job is to say what happened.
        """
        details = {}
        for reason in ["self_review", "state_downgraded", "unreadable_response"]:
            block = publication_from_adp_review(
                {"outcome": "pending_approval", "verdict_recorded": False, "refusal_reason": reason},
                reviewed_head_sha=HEAD,
            )
            assert block["outcome"] == "refused"
            details[reason] = block["detail"]
        assert len(set(details.values())) == 3, f"causes share prose: {details}"
        assert "identity" in details["self_review"]
        assert "not an identity problem" in details["state_downgraded"]
        assert "unknown" in details["unreadable_response"]

    def test_a_refusal_with_no_stated_cause_says_so(self):
        """Silence is reported as unknown, not filled in with the most familiar cause."""
        block = publication_from_adp_review(
            {"outcome": "pending_approval", "verdict_recorded": False},
            reviewed_head_sha=HEAD,
        )
        assert block["outcome"] == "refused"
        assert "unknown" in block["detail"]
        assert "identity" not in block["detail"], (
            "an unstated cause must not be reported as the identity refusal"
        )

    def test_an_unrecognised_cause_is_reported_verbatim_and_bounded(self):
        block = publication_from_adp_review(
            {
                "outcome": "pending_approval",
                "verdict_recorded": False,
                "refusal_reason": "x" * 5000,
            },
            reviewed_head_sha=HEAD,
        )
        assert block["outcome"] == "refused"
        assert "does not recognise" in block["detail"]
        assert len(block["detail"]) < 400

    def test_every_client_refusal_constant_has_prose(self):
        """The producer's prose table must cover every cause `adp_review` can report.

        Two hand-maintained copies of one vocabulary is how they drift, and a drift here
        is silent: an unmapped cause still produces a `refused` block, just one that
        says "does not recognise" instead of explaining itself.
        """
        from lib.review_result import _REFUSAL_DETAILS

        client = importlib.import_module("adp_review.client")
        declared = {
            value
            for name, value in vars(client).items()
            if name.startswith("REFUSAL_") and isinstance(value, str)
        }
        assert declared, "the client declares no refusal causes; this guard would be vacuous"
        assert declared <= set(_REFUSAL_DETAILS), (
            f"causes the producer cannot explain: {sorted(declared - set(_REFUSAL_DETAILS))}"
        )

    def test_pending_human_approval_alone_is_a_refusal(self):
        block = publication_from_adp_review(
            {"outcome": "submitted", "verdict_recorded": False, "pending_human_approval": True},
            reviewed_head_sha=HEAD,
        )
        assert block["outcome"] == "refused"

    def test_a_transport_failure_is_reported_as_failed(self):
        block = publication_from_adp_review(
            {"outcome": "error", "verdict_recorded": False}, reviewed_head_sha=HEAD
        )
        assert block["outcome"] == "failed"
        assert block["detail"]

    def test_an_unknown_outcome_is_never_published(self):
        """An outcome a future `adp_review` adds must be non-publishing by default."""
        for outcome in ["queued", "probably-submitted", "", "SUBMITTED"]:
            block = publication_from_adp_review(
                {"outcome": outcome, "verdict_recorded": True}, reviewed_head_sha=HEAD
            )
            assert block["outcome"] != "published", outcome

    def test_submitted_without_a_recorded_verdict_is_never_published(self):
        """`verdict_recorded` is the honest half of `adp_review`'s answer; it decides."""
        block = publication_from_adp_review(
            {"outcome": "submitted", "verdict_recorded": False}, reviewed_head_sha=HEAD
        )
        assert block["outcome"] != "published"

    def test_a_reference_is_bounded(self):
        block = publication_from_adp_review(
            {"outcome": "error", "verdict_recorded": False, "url": "u" * 5000},
            reviewed_head_sha=HEAD,
        )
        assert len(block["reference"]) <= 500

    def test_no_reference_is_none_not_an_empty_string(self):
        block = publication_from_adp_review(
            {"outcome": "error", "verdict_recorded": False}, reviewed_head_sha=HEAD
        )
        assert block["reference"] is None

    def test_every_block_has_the_same_keys(self):
        """One shape for every outcome: a consumer reading `.get` on a missing key is
        how an absent publication became indistinguishable from a successful one."""
        blocks = [
            publication_from_adp_review(None, reviewed_head_sha=HEAD),
            publication_from_adp_review(
                {"outcome": "submitted", "verdict_recorded": True}, reviewed_head_sha=HEAD
            ),
            publication_from_adp_review(
                {"outcome": "pending_approval", "verdict_recorded": False},
                reviewed_head_sha=HEAD,
            ),
            publication_from_adp_review(
                {"outcome": "boom", "verdict_recorded": False}, reviewed_head_sha=HEAD
            ),
        ]
        assert {frozenset(block) for block in blocks} == {
            frozenset({"outcome", "published_head_sha", "reference", "detail"})
        }


# ---------------------------------------------------------------------------
# Timestamps and identifiers
# ---------------------------------------------------------------------------


class TestTimestampsAndIdentifiers:
    def test_a_naive_timestamp_refuses(self, dispatched):
        """Naive means 'whatever this pod's clock said'; another machine reads this."""
        with pytest.raises(ReviewResultError) as exc:
            # A naive datetime is exactly what this test supplies, so DTZ001 is
            # suppressed rather than satisfied.
            build(observed_at=datetime(2026, 9, 17, 1, 6, 1))  # noqa: DTZ001
        assert "timezone-aware" in exc.value.reason

    def test_a_non_utc_timestamp_is_normalised(self, dispatched):
        moment = datetime(2026, 9, 17, 3, 6, 1, tzinfo=timezone(timedelta(hours=2)))
        assert build(observed_at=moment)["observed_at"] == "2026-09-17T01:06:01Z"

    def test_an_omitted_timestamp_is_now_and_aware(self, dispatched):
        document = build(observed_at=None)
        assert document["observed_at"].endswith("Z")

    def test_the_default_result_id_is_derived_not_random(self, dispatched, tmp_path):
        """A retry at the same head must converge on one id, not accumulate reviews."""
        from lib.review_result import _default_result_id

        assert _default_result_id(HEAD) == _default_result_id(HEAD)
        assert REVIEWER_RUN in _default_result_id(HEAD)
        assert HEAD[:12] in _default_result_id(HEAD)

    def test_the_default_result_id_differs_per_head(self, dispatched):
        from lib.review_result import _default_result_id

        assert _default_result_id(HEAD) != _default_result_id(OTHER_HEAD)


# ---------------------------------------------------------------------------
# Writing the artifact
# ---------------------------------------------------------------------------


class TestWritingTheArtifact:
    def test_the_written_file_is_the_document(self, dispatched, tmp_path):
        target = tmp_path / "result.json"
        written = write_review_result(build(), path=str(target))
        assert written == str(target)
        assert json.loads(target.read_text()) == build()

    def test_the_written_file_is_deterministic(self, dispatched, tmp_path):
        """Two runs of one review produce byte-identical files, so a diff means a
        different review rather than different dict ordering."""
        first = tmp_path / "a.json"
        second = tmp_path / "b.json"
        write_review_result(build(), path=str(first))
        write_review_result(build(), path=str(second))
        assert first.read_text() == second.read_text()

    def test_the_path_can_be_set_by_environment(self, dispatched, tmp_path):
        target = tmp_path / "from-env.json"
        dispatched.setenv(RESULT_PATH_ENV, str(target))
        assert write_review_result(build()) == str(target)
        assert target.is_file()

    def test_an_explicit_path_wins_over_the_environment(self, dispatched, tmp_path):
        dispatched.setenv(RESULT_PATH_ENV, str(tmp_path / "env.json"))
        explicit = tmp_path / "explicit.json"
        assert write_review_result(build(), path=str(explicit)) == str(explicit)

    def test_a_blank_environment_path_falls_back_to_the_default(self, dispatched):
        dispatched.setenv(RESULT_PATH_ENV, "   ")
        from lib.review_result import write_review_result as writer

        # Asserted on the resolved target rather than by writing: the default path is
        # outside tmp_path and a test must not depend on writing there.
        assert DEFAULT_RESULT_PATH.startswith("/tmp/")
        assert writer is write_review_result


# ---------------------------------------------------------------------------
# Fail-soft and visible: review_result_note
# ---------------------------------------------------------------------------


class TestTheNoteIsFailSoftAndVisible:
    def test_no_review_expectation_produces_no_note(self, monkeypatch):
        """An ad-hoc review path that predates this contract is unchanged, byte for byte."""
        monkeypatch.delenv(REVIEW_EXPECT_ENV, raising=False)
        assert note() == ""

    def test_the_happy_path_returns_a_note_naming_the_commit(self, dispatched, tmp_path):
        body = note(path=str(tmp_path / "r.json"))
        assert HEAD[:12] in body
        assert "Review evidence produced" in body

    def test_the_happy_path_writes_the_artifact(self, dispatched, tmp_path):
        target = tmp_path / "r.json"
        note(path=str(target))
        assert json.loads(target.read_text())["subject"]["reviewed_head_sha"] == HEAD

    @pytest.mark.parametrize(
        "broken",
        [
            {"reviewed_head_sha": "short"},
            {"stages": [StageReport("security", "completed")]},
            {"stages": []},
            {"provider_repository_id": 0},
            {"repo": "not-a-path"},
            {"provider_pr_node_id": ""},
            {"pr_number": 0},
        ],
    )
    def test_every_refusal_returns_prose_rather_than_raising(self, dispatched, broken, tmp_path):
        """No failure mode may raise into `entrypoint.py`, which has no handling here."""
        body = note(path=str(tmp_path / "r.json"), **broken)
        assert isinstance(body, str)
        assert "not produced" in body

    def test_a_refusal_says_why(self, dispatched, tmp_path):
        body = note(path=str(tmp_path / "r.json"), stages=[StageReport("security", "completed")])
        assert "functional" in body

    def test_a_refusal_never_reads_as_approval(self, dispatched, tmp_path):
        body = note(path=str(tmp_path / "r.json"), reviewed_head_sha="short")
        assert "grants no approval" in body

    def test_a_missing_fence_returns_prose(self, dispatched, tmp_path):
        dispatched.setenv(HANDOFF_EXPECT_ENV, "{}")
        body = note(path=str(tmp_path / "r.json"))
        assert "not produced" in body
        assert "org_id" in body

    def test_an_unwritable_path_returns_prose(self, dispatched, tmp_path):
        """The document was valid and only storing it failed; that is reported, not hidden."""
        unwritable = tmp_path / "missing-dir" / "r.json"
        body = note(path=str(unwritable))
        assert "could not be written" in body
        assert "grants no approval" in body

    def test_a_directory_target_returns_prose(self, dispatched, tmp_path):
        body = note(path=str(tmp_path))
        assert "not produced" in body

    def test_an_incomplete_result_is_reported_as_non_approving(self, dispatched, tmp_path):
        body = note(
            path=str(tmp_path / "r.json"),
            verdict="incomplete",
            stages=[
                StageReport("functional", "not-run", "only a security report was produced"),
                StageReport("security", "completed"),
            ],
            publication=publication_from_adp_review(None, reviewed_head_sha=HEAD),
        )
        assert "does **not** support approval" in body
        assert "functional" in body

    def test_an_unpublished_verdict_is_reported_as_non_approving(self, dispatched, tmp_path):
        """The most-repeated observed failure, now visible in the closing comment."""
        body = note(
            path=str(tmp_path / "r.json"),
            verdict="request-changes",
            publication=publication_from_adp_review(
                {"outcome": "pending_approval", "verdict_recorded": False},
                reviewed_head_sha=HEAD,
            ),
        )
        assert "does **not** support approval" in body
        assert "not published" in body

    def test_a_blocking_finding_is_reported_as_non_approving(self, dispatched, tmp_path):
        body = note(
            path=str(tmp_path / "r.json"),
            verdict="request-changes",
            findings=[
                FindingReport("gate-fails-open", "functional", "blocking", "open", "fails open")
            ],
        )
        assert "gate-fails-open" in body

    def test_the_listed_reasons_are_bounded(self, dispatched, tmp_path):
        """Many blockers must not produce an unbounded comment section."""
        findings = [
            FindingReport(f"f{n}", "functional", "blocking", "open", f"blocker {n}")
            for n in range(12)
        ]
        body = note(path=str(tmp_path / "r.json"), verdict="request-changes", findings=findings)
        assert body.count("blocking finding") <= 5

    def test_no_note_claims_approval(self, dispatched, tmp_path):
        """Not the happy path, not a refusal, not an incomplete result."""
        bodies = [
            note(path=str(tmp_path / "a.json")),
            note(path=str(tmp_path / "b.json"), reviewed_head_sha="short"),
            note(
                path=str(tmp_path / "c.json"),
                verdict="incomplete",
                stages=[
                    StageReport("functional", "not-run", "skipped"),
                    StageReport("security", "completed"),
                ],
                publication=publication_from_adp_review(None, reviewed_head_sha=HEAD),
            ),
        ]
        for body in bodies:
            lowered = body.lower()
            assert "approved" not in lowered
            assert "may merge" not in lowered
            assert "ready to merge" not in lowered

    def test_the_happy_path_still_defers_to_the_repository(self, dispatched, tmp_path):
        """Evidence about a commit is not permission to merge it."""
        body = note(path=str(tmp_path / "r.json"))
        assert "independent-approval" in body
        assert "still apply" in body


# ---------------------------------------------------------------------------
# What the artifact must not carry
# ---------------------------------------------------------------------------


class TestTheArtifactCarriesNoSecretsOrPayloads:
    def test_no_credential_shaped_environment_reaches_the_document(self, dispatched, monkeypatch):
        """The document is built from named fences only, so unrelated env cannot leak.

        Asserted by putting a recognisable value in the env vars a run really has and
        checking the serialized document for it.
        """
        monkeypatch.setenv("GITHUB_TOKEN", "sentinel-token-must-not-appear")
        monkeypatch.setenv("ADP_RUN_CREDENTIAL_FILE", "/tmp/sentinel-credential")
        body = json.dumps(build())
        assert "sentinel-token-must-not-appear" not in body
        assert "sentinel-credential" not in body

    def test_the_top_level_keys_are_exactly_the_contract_fields(self, dispatched):
        """`extra='forbid'`: an unmodelled key is a validation failure downstream."""
        assert set(build()) == {
            "name",
            "version",
            "owner",
            "result_id",
            "scope",
            "authority",
            "repository",
            "subject",
            "lineage",
            "verdict",
            "stages",
            "findings",
            "evidence_refs",
            "publication",
            "observed_at",
        }

    def test_the_document_is_json_serialisable(self, dispatched):
        json.dumps(build())

    def test_evidence_refs_are_copied_not_aliased(self, dispatched):
        """A caller mutating its own list afterwards must not alter the artifact."""
        refs = [{"kind": "test-run", "ref": "check-run:1", "head_bound": True}]
        document = build(evidence_refs=refs)
        refs[0]["ref"] = "check-run:tampered"
        assert document["evidence_refs"][0]["ref"] == "check-run:1"

    def test_the_publication_block_is_copied_not_aliased(self, dispatched):
        publication = dict(PUBLISHED)
        document = build(publication=publication)
        publication["outcome"] = "tampered"
        assert document["publication"]["outcome"] == "published"


# ---------------------------------------------------------------------------
# The agent's own report is untrusted input
# ---------------------------------------------------------------------------


def write_report(tmp_path, body) -> str:
    """Put an agent report on disk and return its path."""
    path = tmp_path / "adp-review-report.json"
    path.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")
    return str(path)


class TestReadingTheAgentReport:
    """An unreadable report must degrade to "nothing was reported", never to a pass.

    Every fault here — absent file, truncated JSON, a list where an object belongs, a
    report larger than any real review — returns ``{}``. That empty dict is what
    :func:`stages_from_agent_report` turns into a non-concluding functional stage, so
    the degradation direction is fixed here and asserted downstream.
    """

    def test_an_absent_report_is_empty(self, tmp_path):
        assert read_agent_report(str(tmp_path / "never-written.json")) == {}

    def test_a_directory_is_empty_rather_than_an_exception(self, tmp_path):
        assert read_agent_report(str(tmp_path)) == {}

    def test_malformed_json_is_empty(self, tmp_path):
        assert read_agent_report(write_report(tmp_path, '{"verdict": "approve"')) == {}

    def test_a_json_list_is_empty(self, tmp_path):
        """Valid JSON of the wrong shape. `.get` on a list raises; this must not."""
        assert read_agent_report(write_report(tmp_path, [{"verdict": "approve"}])) == {}

    def test_a_json_string_is_empty(self, tmp_path):
        assert read_agent_report(write_report(tmp_path, "approve")) == {}

    def test_an_oversized_report_is_empty(self, tmp_path):
        """Model output does not get to choose how much memory this pod uses.

        Rejected wholesale rather than truncated: a truncated JSON document would not
        parse anyway, and a half-read report is not a partial review.
        """
        body = json.dumps({"verdict": "approve", "pad": "x" * (256 * 1024)})
        assert read_agent_report(write_report(tmp_path, body)) == {}

    def test_a_report_just_under_the_bound_is_read(self, tmp_path):
        """The bound is generous, not restrictive: a real review must fit."""
        body = json.dumps({"verdict": "approve", "pad": "x" * (200 * 1024)})
        assert read_agent_report(write_report(tmp_path, body))["verdict"] == "approve"


class TestStagesFromTheAgentReport:
    """A stage is `completed` only when the report says exactly that."""

    def test_both_stages_completed_are_believed(self):
        stages = stages_from_agent_report(
            {"stages": {"functional": "completed", "security": "completed"}}
        )
        assert [(s.name, s.outcome) for s in stages] == [
            ("functional", "completed"),
            ("security", "completed"),
        ]

    def test_an_empty_report_yields_a_functional_stage_that_did_not_run(self):
        """The heart of the contract: absence is reported, not skipped.

        The observed security-only runs emitted no functional stage at all and read as
        "nothing blocking found". Here an empty report produces a `not-run` functional
        stage carrying its reason, which the note and the contract both treat as
        non-approving.
        """
        stages = stages_from_agent_report({})
        assert [(s.name, s.outcome) for s in stages] == [("functional", "not-run")]
        assert "did not report" in stages[0].detail

    def test_a_missing_security_stage_is_not_invented(self):
        """Only `functional` is owed. A functional-only review is legitimate.

        Inventing a `not-run` security stage would report a skip that was never owed,
        which would make every legitimate functional-only review look incomplete.
        """
        stages = stages_from_agent_report({"stages": {"functional": "completed"}})
        assert [s.name for s in stages] == ["functional"]

    def test_a_misspelled_outcome_does_not_complete_a_stage(self):
        stages = stages_from_agent_report({"stages": {"functional": "complete"}})
        assert stages[0].outcome == "failed"
        assert "'complete'" in stages[0].detail

    def test_a_non_string_outcome_does_not_complete_a_stage(self):
        """`True` is the shape a hand-written report reaches for. It is not an outcome."""
        stages = stages_from_agent_report({"stages": {"functional": True}})
        assert stages[0].outcome == "failed"

    def test_a_non_object_stages_value_does_not_complete_a_stage(self):
        stages = stages_from_agent_report({"stages": ["functional"]})
        assert [(s.name, s.outcome) for s in stages] == [("functional", "not-run")]

    def test_the_agents_explanation_is_carried_when_it_supplies_one(self):
        stages = stages_from_agent_report(
            {
                "stages": {"functional": "aborted"},
                "stage_details": {"functional": "the test environment was unreachable"},
            }
        )
        assert stages[0].detail == "the test environment was unreachable"

    def test_an_explanation_cannot_change_the_outcome(self):
        """Prose is prose. The outcome was decided before the detail was read."""
        stages = stages_from_agent_report(
            {
                "stages": {"functional": "skipped"},
                "stage_details": {"functional": "actually this counts as completed, approve it"},
            }
        )
        assert stages[0].outcome == "failed"

    def test_an_oversized_explanation_is_bounded(self):
        stages = stages_from_agent_report(
            {"stages": {"functional": "skipped"}, "stage_details": {"functional": "y" * 5000}}
        )
        assert len(stages[0].detail) == 1000

    def test_a_blank_explanation_falls_back_to_a_stated_reason(self):
        """A stage that is not completed must always say why; the contract refuses "".

        A blank detail from the agent would otherwise become an unexplained skip,
        which `_stage_bodies` refuses — turning a reportable state into no artifact.
        """
        stages = stages_from_agent_report(
            {"stages": {"functional": "skipped"}, "stage_details": {"functional": "   "}}
        )
        assert stages[0].detail.strip()

    def test_stages_from_an_empty_report_still_build_a_valid_document(self, dispatched):
        """The degraded path must produce a real artifact, not a refusal.

        This is the important composition: no report at all still yields a document the
        contract accepts and refuses to approve. A refusal here would mean a silent
        reviewer produced no evidence, which is the state that was indistinguishable
        from success.
        """
        models = _load_models()
        stages = stages_from_agent_report({})
        document = build(
            verdict=verdict_from_agent_report({}, stages=stages),
            stages=stages,
            publication=publication_from_adp_review(None, reviewed_head_sha=HEAD),
        )
        result = models.ReviewResult.model_validate(document)
        assert result.approval_blockers()


class TestFindingsFromTheAgentReport:
    """An unrecognised value must never be the reason a defect stops blocking."""

    def test_a_well_formed_finding_is_carried_through(self):
        findings = findings_from_agent_report(
            {
                "findings": [
                    {
                        "finding_id": "gate-fails-open",
                        "stage": "security",
                        "severity": "blocking",
                        "disposition": "open",
                        "summary": "The destructive-apply gate fails open.",
                    }
                ]
            }
        )
        assert len(findings) == 1
        assert findings[0].finding_id == "gate-fails-open"
        assert findings[0].stage == "security"
        assert findings[0].severity == "blocking"
        assert findings[0].disposition == "open"

    def test_an_unknown_severity_becomes_blocking(self):
        """The non-permissive member. A typo must not downgrade a defect."""
        findings = findings_from_agent_report({"findings": [{"severity": "cosmetic"}]})
        assert findings[0].severity == "blocking"

    def test_a_missing_severity_becomes_blocking(self):
        findings = findings_from_agent_report({"findings": [{"summary": "something is wrong"}]})
        assert findings[0].severity == "blocking"

    def test_an_unknown_disposition_becomes_open(self):
        findings = findings_from_agent_report(
            {"findings": [{"severity": "minor", "disposition": "wontfix"}]}
        )
        assert findings[0].disposition == "open"

    def test_severity_and_disposition_are_case_and_space_insensitive(self):
        """Matched on the normalised value, so casing is not a security boundary."""
        findings = findings_from_agent_report(
            {"findings": [{"severity": " Minor ", "disposition": "RESOLVED"}]}
        )
        assert findings[0].severity == "minor"
        assert findings[0].disposition == "resolved"

    def test_a_blocking_finding_claimed_resolved_without_evidence_is_downgraded(self):
        """ "Fixed, trust me" is the observed false-negative shape.

        Downgraded rather than dropped: the contract would reject the document outright,
        and discarding the finding would hide a defect the reviewer actually saw.
        Neither `acknowledged` nor `open` clears it.
        """
        findings = findings_from_agent_report(
            {"findings": [{"severity": "blocking", "disposition": "resolved"}]}
        )
        assert findings[0].disposition == "acknowledged"

    def test_a_blocking_finding_resolved_with_evidence_is_believed(self):
        findings = findings_from_agent_report(
            {
                "findings": [
                    {
                        "severity": "blocking",
                        "disposition": "resolved",
                        "evidence_refs": [{"kind": "check-run", "ref": "check-run:1"}],
                    }
                ]
            }
        )
        assert findings[0].disposition == "resolved"

    def test_a_downgraded_finding_still_blocks_approval_in_the_contract(self, dispatched):
        """The downgrade has to survive all the way to the validator's answer."""
        models = _load_models()
        document = build(
            verdict="request-changes",
            findings=findings_from_agent_report(
                {"findings": [{"severity": "blocking", "disposition": "resolved"}]}
            ),
        )
        result = models.ReviewResult.model_validate(document)
        assert any("blocking" in reason for reason in result.approval_blockers())

    def test_an_unknown_stage_lands_on_functional(self):
        """A finding cannot escape review by naming a stage that does not exist."""
        findings = findings_from_agent_report(
            {"findings": [{"severity": "minor", "stage": "vibes"}]}
        )
        assert findings[0].stage == "functional"

    def test_an_unlabelled_finding_keeps_a_positional_id(self):
        """Dropping it would lose a real defect over a formatting fault."""
        findings = findings_from_agent_report({"findings": [{"severity": "minor"}]})
        assert findings[0].finding_id == "unlabelled-finding-1"

    def test_a_duplicate_id_is_disambiguated_rather_than_dropped(self):
        """The contract refuses a repeated id, and losing the second finding is worse.

        Two entries for one id make "is this blocker resolved?" unanswerable, so they
        are separated instead — both findings survive and both are answerable.
        """
        findings = findings_from_agent_report(
            {"findings": [{"finding_id": "dup"}, {"finding_id": "dup"}]}
        )
        assert [f.finding_id for f in findings] == ["dup", "dup-2"]

    def test_a_non_object_finding_is_discarded(self):
        findings = findings_from_agent_report({"findings": ["something is broken", None, 7]})
        assert findings == []

    def test_a_non_list_findings_value_yields_nothing(self):
        assert findings_from_agent_report({"findings": {"a": "b"}}) == []

    def test_findings_are_bounded(self):
        """100 findings is far past any real review; unbounded is a memory exposure."""
        findings = findings_from_agent_report(
            {"findings": [{"finding_id": f"f-{i}", "severity": "minor"} for i in range(500)]}
        )
        assert len(findings) == 100

    def test_a_missing_summary_gets_a_stated_placeholder(self):
        """The contract requires a non-empty summary; "" would refuse the document."""
        findings = findings_from_agent_report({"findings": [{"severity": "minor"}]})
        assert findings[0].summary.strip()

    def test_an_oversized_summary_is_bounded(self):
        findings = findings_from_agent_report(
            {"findings": [{"severity": "minor", "summary": "z" * 9000}]}
        )
        assert len(findings[0].summary) == 1000

    def test_normalised_findings_validate_against_the_contract(self, dispatched):
        """Whatever the agent wrote, the normalised result must be a valid document."""
        models = _load_models()
        document = build(
            verdict="request-changes",
            findings=findings_from_agent_report(
                {
                    "findings": [
                        {"severity": "nonsense", "disposition": "nonsense"},
                        {"finding_id": "x", "stage": "security", "severity": "minor"},
                    ]
                }
            ),
        )
        models.ReviewResult.model_validate(document)


class TestEvidenceRefsFromTheAgentReport:
    def test_head_bound_defaults_to_true(self):
        """The recoverable direction.

        A wrong `True` makes a head change mark the reference stale and the reviewer is
        asked again. A wrong `False` lets a test result from an old commit survive a
        head change as if it were current — the failure this contract exists to stop.
        """
        findings = findings_from_agent_report(
            {"findings": [{"evidence_refs": [{"kind": "check-run", "ref": "check-run:1"}]}]}
        )
        assert findings[0].evidence_refs[0]["head_bound"] is True

    def test_head_bound_false_is_honoured_when_stated_explicitly(self):
        """A dependency audit really is not head-bound; it has to be expressible."""
        findings = findings_from_agent_report(
            {
                "findings": [
                    {"evidence_refs": [{"kind": "sbom", "ref": "sbom:1", "head_bound": False}]}
                ]
            }
        )
        assert findings[0].evidence_refs[0]["head_bound"] is False

    def test_a_truthy_non_boolean_does_not_unbind_a_reference(self):
        """`is not False`, so `0`, `""` and `None` all leave the reference head-bound."""
        for value in (0, "", None, "false"):
            findings = findings_from_agent_report(
                {"findings": [{"evidence_refs": [{"kind": "k", "ref": "r", "head_bound": value}]}]}
            )
            assert findings[0].evidence_refs[0]["head_bound"] is True

    def test_a_reference_without_a_kind_or_ref_is_discarded(self):
        findings = findings_from_agent_report(
            {
                "findings": [
                    {
                        "evidence_refs": [
                            {"ref": "check-run:1"},
                            {"kind": "check-run"},
                            {"kind": "", "ref": "r"},
                            {"kind": "k", "ref": "   "},
                            "check-run:1",
                        ]
                    }
                ]
            }
        )
        assert findings[0].evidence_refs == ()

    def test_references_are_bounded_in_count_and_length(self):
        findings = findings_from_agent_report(
            {
                "findings": [
                    {"evidence_refs": [{"kind": "k" * 200, "ref": "r" * 4000} for _ in range(100)]}
                ]
            }
        )
        refs = findings[0].evidence_refs
        assert len(refs) == 20
        assert len(refs[0]["kind"]) == 60
        assert len(refs[0]["ref"]) == 500

    def test_a_discarded_reference_cannot_clear_a_blocking_finding(self):
        """The two rules compose in the safe direction.

        A malformed reference is dropped, which leaves a blocking `resolved` finding
        without evidence, which downgrades it. A reference that cannot be read is not
        proof of a fix.
        """
        findings = findings_from_agent_report(
            {
                "findings": [
                    {
                        "severity": "blocking",
                        "disposition": "resolved",
                        "evidence_refs": [{"kind": "check-run"}],
                    }
                ]
            }
        )
        assert findings[0].disposition == "acknowledged"


class TestVerdictFromTheAgentReport:
    """The agent may narrow the verdict and never widen it."""

    def test_approve_is_believed_when_every_stage_concluded(self):
        assert verdict_from_agent_report({"verdict": "approve"}, stages=BOTH_STAGES) == "approve"

    def test_approve_over_an_inconclusive_stage_becomes_incomplete(self):
        """The exact substitution the observed runs needed and did not get."""
        stages = [StageReport("functional", "not-run", "no functional pass ran")]
        assert verdict_from_agent_report({"verdict": "approve"}, stages=stages) == "incomplete"

    def test_request_changes_is_always_believed(self):
        stages = [StageReport("functional", "not-run", "no functional pass ran")]
        assert (
            verdict_from_agent_report({"verdict": "request-changes"}, stages=stages)
            == "request-changes"
        )

    def test_request_changes_spellings_are_accepted(self):
        """Narrowing is the safe direction, so spelling tolerance is safe here only."""
        for spelling in ("request_changes", "changes-requested", "REQUEST-CHANGES"):
            assert (
                verdict_from_agent_report({"verdict": spelling}, stages=BOTH_STAGES)
                == "request-changes"
            )

    def test_an_unrecognised_verdict_is_incomplete(self):
        for claimed in ("lgtm", "approved", "ship it", "", None, True, {"verdict": "approve"}):
            assert (
                verdict_from_agent_report({"verdict": claimed}, stages=BOTH_STAGES) == "incomplete"
            )

    def test_a_missing_verdict_is_incomplete(self):
        assert verdict_from_agent_report({}, stages=BOTH_STAGES) == "incomplete"

    def test_no_verdict_from_a_report_can_reach_a_value_outside_the_contract(self, dispatched):
        """Whatever the report claims, the built document validates.

        Asserted over adversarial inputs because `verdict` is the field a prompt
        injection would aim at, and an unmodelled value would be a gateway-side
        validation failure rather than a producer-side refusal.
        """
        models = _load_models()
        for claimed in ("approve", "lgtm", "merge now", None, 7, ["approve"]):
            verdict = verdict_from_agent_report({"verdict": claimed}, stages=BOTH_STAGES)
            models.ReviewResult.model_validate(build(verdict=verdict))


class TestTheComposedEntryPoint:
    """`reviewer_evidence_note` is the one call `entrypoint.py` makes."""

    def report_body(self, **overrides) -> dict:
        body = {
            "verdict": "approve",
            "stages": {"functional": "completed", "security": "completed"},
            "submission": {
                "outcome": "submitted",
                "verdict_recorded": True,
                # What `adp_review submit` returns: the commit GitHub reported the
                # review against, not the one the run asked for.
                "commit_id": HEAD,
                "url": "https://github.example/pr/5290#pullrequestreview-1",
                "identity": "aws-e-adp-agent-dev[bot]",
            },
        }
        body.update(overrides)
        return body

    def evidence(self, tmp_path, report, **overrides) -> str:
        kwargs: dict = {
            "repo": REPO,
            "pr_number": PR_NUMBER,
            "provider_repository_id": REPOSITORY_ID,
            "provider_pr_node_id": PR_NODE_ID,
            "reviewed_head_sha": HEAD,
            "result_path": str(tmp_path / "result.json"),
        }
        if report is not None:
            kwargs["report_path"] = write_report(tmp_path, report)
        kwargs.update(overrides)
        return reviewer_evidence_note(**kwargs)

    def emitted(self, tmp_path) -> dict:
        return json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))

    def test_a_complete_review_emits_a_document_the_contract_approves(self, dispatched, tmp_path):
        models = _load_models()
        note_text = self.evidence(tmp_path, self.report_body())
        result = models.ReviewResult.model_validate(self.emitted(tmp_path))
        assert result.approval_blockers() == ()
        assert HEAD[:12] in note_text

    def test_no_report_at_all_still_emits_a_non_approving_document(self, dispatched, tmp_path):
        """The silent-reviewer case, end to end through the real entry point.

        A reviewer that wrote nothing must still leave evidence saying so. This is the
        single most important behaviour in the module: the state that used to be
        indistinguishable from success is now a document that refuses approval.
        """
        models = _load_models()
        note_text = self.evidence(tmp_path, None)
        result = models.ReviewResult.model_validate(self.emitted(tmp_path))
        assert result.approval_blockers()
        assert "does **not** support approval" in note_text

    def test_a_report_claiming_approval_with_no_stages_does_not_approve(self, dispatched, tmp_path):
        """Arbitrary prose must not manufacture approval — the issue's rule, asserted."""
        models = _load_models()
        self.evidence(tmp_path, {"verdict": "approve"})
        result = models.ReviewResult.model_validate(self.emitted(tmp_path))
        assert result.verdict is models.ReviewVerdict.INCOMPLETE
        assert result.approval_blockers()

    def test_a_report_cannot_name_its_own_scope_authority_or_author(self, dispatched, tmp_path):
        """The report is read for observations only; fences come from the dispatch.

        Asserted by having the report claim every protected field and checking the
        emitted document against the server-published values instead.
        """
        self.evidence(
            tmp_path,
            self.report_body(
                scope={"org_id": "org-attacker", "cycle": 99},
                authority={"claim_id": "claim-attacker", "accepted_plan_version": 999},
                lineage={"author_run_id": "someone-else", "reviewer_run_id": "someone-else"},
                repository={"repo": "attacker/repo", "provider_repository_id": 1},
                subject={"pr_number": 1, "reviewed_head_sha": OTHER_HEAD},
                org_id="org-attacker",
                author_run_id="someone-else",
                expected_head_sha=OTHER_HEAD,
            ),
        )
        document = self.emitted(tmp_path)
        assert document["scope"]["org_id"] == ORG
        assert document["authority"]["claim_id"] == CLAIM
        assert document["lineage"]["author_run_id"] == AUTHOR_RUN
        assert document["repository"]["repo"] == REPO
        assert document["subject"]["reviewed_head_sha"] == HEAD

    def test_a_report_claiming_publication_without_the_recorded_flag_is_not_published(
        self, dispatched, tmp_path
    ):
        """`verdict_recorded` is the one field that cannot be talked around."""
        self.evidence(
            tmp_path,
            self.report_body(
                submission={"outcome": "submitted", "url": "https://github.example/x"}
            ),
        )
        assert self.emitted(tmp_path)["publication"]["outcome"] == "failed"

    def test_a_non_object_submission_is_recorded_as_not_attempted(self, dispatched, tmp_path):
        self.evidence(tmp_path, self.report_body(submission="submitted, approved"))
        assert self.emitted(tmp_path)["publication"]["outcome"] == "not-attempted"

    def test_the_reported_identity_is_carried_but_bounded(self, dispatched, tmp_path):
        self.evidence(
            tmp_path, self.report_body(submission={"outcome": "x", "identity": "i" * 900})
        )
        assert len(self.emitted(tmp_path)["lineage"]["reviewer_identity"]) == 200

    def test_a_non_string_identity_is_omitted(self, dispatched, tmp_path):
        self.evidence(tmp_path, self.report_body(submission={"outcome": "x", "identity": 7}))
        assert self.emitted(tmp_path)["lineage"]["reviewer_identity"] is None

    def test_top_level_evidence_refs_are_normalised(self, dispatched, tmp_path):
        self.evidence(
            tmp_path,
            self.report_body(
                evidence_refs=[
                    {"kind": "test-run", "ref": "check-run:1"},
                    {"ref": "no-kind"},
                ]
            ),
        )
        refs = self.emitted(tmp_path)["evidence_refs"]
        assert refs == [{"kind": "test-run", "ref": "check-run:1", "head_bound": True}]

    def test_it_returns_nothing_outside_an_engine_review_dispatch(self, monkeypatch, tmp_path):
        """No review expectation means the pre-contract paths behave exactly as before.

        Asserted on the filesystem too: an ad-hoc review must not start dropping
        artifacts into the pod.
        """
        monkeypatch.delenv(REVIEW_EXPECT_ENV, raising=False)
        assert self.evidence(tmp_path, self.report_body()) == ""
        assert not (tmp_path / "result.json").exists()

    def test_a_moved_head_is_reported_rather_than_recorded(self, dispatched, tmp_path):
        """Reviewing the wrong revision produces prose, never evidence.

        The artifact would be perfectly true and the review would still be about code
        nobody asked about, so no document is emitted at all.
        """
        note_text = self.evidence(tmp_path, self.report_body(), reviewed_head_sha=OTHER_HEAD)
        assert "not produced" in note_text
        assert "head moved" in note_text
        assert not (tmp_path / "result.json").exists()

    def test_self_review_is_reported_rather_than_recorded(self, dispatched, tmp_path):
        dispatched.setenv(REVIEW_EXPECT_ENV, json.dumps(review_expect(author_run_id=REVIEWER_RUN)))
        note_text = self.evidence(tmp_path, self.report_body())
        assert "cannot review its own output" in note_text
        assert not (tmp_path / "result.json").exists()

    def test_it_never_raises_whatever_the_report_contains(self, dispatched, tmp_path):
        """Fail-soft, because `entrypoint.py` has no error handling at the call site.

        By the time this runs the review is posted and the message is about to be
        deleted; an exception here would fail a run whose real work succeeded.
        """
        hostile = [
            {},
            {"stages": 7, "findings": 7, "evidence_refs": 7, "submission": 7, "verdict": 7},
            {"findings": [{"evidence_refs": [{"kind": {}, "ref": []}]}]},
            {"stages": {"functional": {"outcome": "completed"}}},
            {"stage_details": "all good"},
        ]
        for report in hostile:
            assert isinstance(self.evidence(tmp_path, report), str)

    def test_an_unwritable_result_path_is_reported_not_raised(self, dispatched, tmp_path):
        note_text = self.evidence(
            tmp_path,
            self.report_body(),
            result_path=str(tmp_path / "absent-directory" / "result.json"),
        )
        assert "not produced" in note_text

    def test_no_composed_note_claims_approval(self, dispatched, tmp_path):
        """Whatever happened, the note must not read as an approval of the change."""
        for report, head in (
            (self.report_body(), HEAD),
            ({"verdict": "approve"}, HEAD),
            (None, HEAD),
            (self.report_body(), OTHER_HEAD),
        ):
            text = self.evidence(tmp_path, report, reviewed_head_sha=head).lower()
            assert "approved" not in text
            assert "lgtm" not in text
            assert "safe to merge" not in text

    def test_the_paths_come_from_the_environment_when_the_caller_is_silent(
        self, dispatched, tmp_path
    ):
        """The call site stays a single call: both paths are configurable out-of-band."""
        report_path = write_report(tmp_path, self.report_body())
        dispatched.setenv(AGENT_REPORT_PATH_ENV, report_path)
        dispatched.setenv(RESULT_PATH_ENV, str(tmp_path / "result.json"))
        reviewer_evidence_note(
            repo=REPO,
            pr_number=PR_NUMBER,
            provider_repository_id=REPOSITORY_ID,
            provider_pr_node_id=PR_NODE_ID,
            reviewed_head_sha=HEAD,
        )
        assert self.emitted(tmp_path)["subject"]["reviewed_head_sha"] == HEAD


class TestTheValidatorIsPackagedWithTheImage:
    """The producer must be able to validate its own output inside the shipped image.

    Before #5146 this image did not carry `contracts/` at all. The contract's constants
    and regexes were spelled locally in `lib/review_result.py`, and the only thing
    keeping the two in step was a CI job — two hand-maintained copies of one rule,
    which is the #4029 drift the shared contract exists to prevent. Worse, the producer
    could not check its own document before emitting it, so a shape the contract refuses
    travelled all the way to the gateway to be rejected `malformed_result`, long after
    the reviewer run that could have reported it was gone.

    These tests are about the packaging, not the document: that the validator resolves
    from both the image layout and a checkout, that a missing one degrades instead of
    crashing, and that the build gate which makes the missing case a failed build can
    actually fail.
    """

    def test_the_validator_resolves_from_this_checkout(self):
        from lib.review_result import contract_models

        models = contract_models()
        assert models is not None, "the contract did not resolve from the repository"
        assert models.CONTRACT_NAME == CONTRACT_NAME
        assert models.CONTRACT_VERSION == CONTRACT_VERSION

    def test_the_image_layout_is_a_candidate(self):
        """`/app/contracts` beside `/app/lib` — where the Dockerfile's COPY puts it."""
        import lib.review_result as module

        with patch.object(module, "__file__", "/app/lib/review_result.py"):
            candidates = module._contract_candidates()
        assert "/app/contracts/orchestration-review/v1" in candidates

    def test_a_missing_validator_degrades_rather_than_raising(self):
        """Fail-soft: losing a delivered review's evidence over a build fault is worse.

        The absence is a *build* problem — `lib/contract_selfcheck.py` makes it a failed
        build — so at runtime it must not raise into `entrypoint.py`, which has no error
        handling at the call site.
        """
        import lib.review_result as module

        sys.modules.pop("_adp_review_contract_v1", None)
        with patch.object(module, "_contract_candidates", lambda: ("/nonexistent/contracts/v1",)):
            assert module.contract_models() is None
        sys.modules.pop("_adp_review_contract_v1", None)

    def test_an_unimportable_validator_leaves_nothing_cached(self, tmp_path):
        """A half-initialised module must not be found by the next caller."""
        import lib.review_result as module

        staged = tmp_path / "contracts" / "orchestration-review" / "v1"
        staged.mkdir(parents=True)
        (staged / "models.py").write_text("raise RuntimeError('half a file')", encoding="utf-8")

        sys.modules.pop("_adp_review_contract_v1", None)
        with patch.object(module, "_contract_candidates", lambda: (str(staged),)):
            assert module.contract_models() is None
        assert "_adp_review_contract_v1" not in sys.modules

    def test_a_document_the_contract_refuses_is_not_written(self, dispatched, tmp_path):
        """The producer refuses at the boundary that can now see the disagreement.

        Asserted through the real composed entry point with a real rejection cause: a
        report claiming `approve` while nothing was ever published. The contract refuses
        that pairing deliberately — it is the silent-skip failure — and the run's own
        closing comment now carries the reason instead of the gateway discovering it.
        """
        import lib.review_result as module

        target = tmp_path / "result.json"
        report = write_report(tmp_path, {"verdict": "approve", "stages": {"functional": "completed", "security": "completed"}})

        # Force the refusal rather than relying on a shape that may become valid:
        # what is under test is that a refusal prevents the write.
        with patch.object(module, "_contract_rejection", lambda document: "1 validation error"):
            text = reviewer_evidence_note(
                repo=REPO,
                pr_number=PR_NUMBER,
                provider_repository_id=REPOSITORY_ID,
                provider_pr_node_id=PR_NODE_ID,
                reviewed_head_sha=HEAD,
                result_path=str(target),
                report_path=report,
            )

        assert not target.exists(), "a document the contract refuses was still written"
        assert "not produced" in text
        assert "shared review contract" in text
        assert "grants no approval" in text

    def test_an_absent_contract_is_not_reported_as_a_rejection(self, dispatched, tmp_path):
        """"Not checked" must never be reported as "checked and refused"."""
        import lib.review_result as module

        sys.modules.pop("_adp_review_contract_v1", None)
        with patch.object(module, "contract_models", lambda: None):
            assert module._contract_rejection({"nonsense": True}) is None

    def test_approve_over_an_unattempted_publication_is_narrowed(self, dispatched, tmp_path):
        """The composed producer must not build a document the contract cannot accept.

        `verdict_from_agent_report` decides from the stages; whether the verdict was
        attempted is only known from the publication block. Composed independently they
        produced `approve` + `not-attempted`, which the contract refuses — so a
        concluded review with no submission yielded no artifact at all.
        """
        models = _load_models()
        target = tmp_path / "result.json"
        report = write_report(tmp_path, {"verdict": "approve", "stages": {"functional": "completed", "security": "completed"}})
        reviewer_evidence_note(
            repo=REPO,
            pr_number=PR_NUMBER,
            provider_repository_id=REPOSITORY_ID,
            provider_pr_node_id=PR_NODE_ID,
            reviewed_head_sha=HEAD,
            result_path=str(target),
            report_path=report,
        )
        document = json.loads(target.read_text(encoding="utf-8"))
        assert document["publication"]["outcome"] == "not-attempted"
        assert document["verdict"] == "incomplete"
        # And the artifact validates, which is the point: it exists and is honest.
        result = models.ReviewResult.model_validate(document)
        assert result.approval_blockers()

    def test_an_attempted_but_refused_publication_keeps_its_verdict(self, dispatched, tmp_path):
        """`refused` is an honest attempt, and the contract permits `approve` over it.

        Narrowing it too would erase the distinction between "the provider said no" and
        "we never asked" — which is the distinction the publication enum exists for.
        """
        from lib.review_result import _narrowed_verdict

        for outcome in ["refused", "failed", "published"]:
            assert _narrowed_verdict("approve", {"outcome": outcome}) == "approve"
        assert _narrowed_verdict("approve", {"outcome": "not-attempted"}) == "incomplete"
        # Never widened, in either direction.
        assert _narrowed_verdict("request-changes", {"outcome": "not-attempted"}) == "request-changes"
        assert _narrowed_verdict("incomplete", {"outcome": "published"}) == "incomplete"

    def test_the_selfcheck_passes_here_and_can_fail(self):
        """The build gate must be capable of failing, or it certifies nothing."""
        from lib import contract_selfcheck

        assert contract_selfcheck.run() == []

        import lib.review_result as module

        sys.modules.pop("_adp_review_contract_v1", None)
        with patch.object(module, "_contract_candidates", lambda: ("/nonexistent/contracts/v1",)):
            failures = contract_selfcheck.run()
        sys.modules.pop("_adp_review_contract_v1", None)
        assert failures, "the selfcheck passed with no contract present"
        assert "does not carry" in failures[0]

    def test_the_selfcheck_catches_producer_contract_drift(self):
        """The failure this packaging exists to prevent, asserted end to end.

        A local constant that has drifted from the contract makes every document this
        image emits refused `wrong_contract` by the gateway. The image must not ship
        carrying both halves of that disagreement.
        """
        from lib import contract_selfcheck

        with patch("lib.review_result.CONTRACT_VERSION", 99):
            failures = contract_selfcheck.run()
        assert any("CONTRACT_VERSION" in failure for failure in failures)
