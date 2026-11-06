"""A CI-only required-checks snapshot must not veto the reviewer's complete verdict for the same head (#7639)."""

import json
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration import merge_review
from src.orchestration.review_cycle import CycleBlockedError

HEAD = "2c3fbe446e51a1c2ec0f99a57be28c903c8f40f6"
REVIEWER = "b9a43edf-16fc-5cc3-aade-ecae3eb113e9"
AUTHOR = "orch:b3029f03-46cd-5bd8-947f-5148062c492e"


def _row(row_id, *, complete, findings, artifact="s3://bucket/review.json#sha256=" + "a" * 64):
    cycle = {"author_run_id": AUTHOR, "reviewer_run_id": REVIEWER, "findings": findings}
    return SimpleNamespace(
        id=row_id,
        artifact_ref=artifact,
        detail={
            "cycle_input": json.dumps(cycle),
            "reviewed_head_sha": HEAD,
            "complete_review": "true" if complete else "false",
            "publication_outstanding": "false",
            "verdict": "approve" if complete else "request-changes",
        },
    )


def _ci_finding(state="failed"):
    summary = {
        "source": "required-checks",
        "summary": "Canonical CI observation",
        "evidence": {"state": state, "checks": [{"name": "Agent control tests", "state": "failure"}]},
    }
    return {"finding_id": "delivery-blocked", "severity": "blocking", "disposition": "open", "stage": "functional", "summary": json.dumps(summary)}


def test_ci_observation_only_recognises_the_harness_snapshot_shape():
    assert merge_review.ci_observation_only(_row(1, complete=False, findings=[_ci_finding()]).detail)
    assert not merge_review.ci_observation_only(_row(2, complete=False, findings=[]).detail)
    defect = {"finding_id": "null-deref", "severity": "blocking", "disposition": "open", "summary": "crash"}
    assert not merge_review.ci_observation_only(_row(3, complete=False, findings=[_ci_finding(), defect]).detail)
    assert not merge_review.ci_observation_only({"cycle_input": "not json"})
    # Live rows store the finding summary cut at ~1000 characters, leaving
    # unterminated JSON; the shape is still recognisable from its prefix.
    truncated = _ci_finding()
    truncated["summary"] = truncated["summary"][:60]
    assert merge_review.ci_observation_only(_row(4, complete=False, findings=[truncated]).detail)
    other = dict(truncated, summary='{"source":"reviewer-judgement","summary":"cut off')
    assert not merge_review.ci_observation_only(_row(5, complete=False, findings=[other]).detail)


@dataclass
class Evidence:
    is_complete_review: bool = True
    publication_blockers: tuple = ()
    artifact_ref: str | None = None


class Session:
    def __init__(self, rows):
        self.rows = rows

    async def scalars(self, _query):
        return SimpleNamespace(all=lambda: list(self.rows))


def _context():
    return SimpleNamespace(execution=SimpleNamespace(id="exec-1"), identity=SimpleNamespace())


def _patch(monkeypatch):
    monkeypatch.setattr(merge_review, "read_review_document", lambda *a, **k: {"document": True})
    monkeypatch.setattr(merge_review, "parse_review_result", lambda document: SimpleNamespace())
    monkeypatch.setattr(merge_review, "_head_bound_refs", lambda result: [])
    monkeypatch.setattr(merge_review, "current_author_run", AsyncMock(return_value=AUTHOR))
    monkeypatch.setattr(merge_review, "validate_review_result", lambda document, **kwargs: Evidence())
    monkeypatch.setattr(merge_review, "require_verified_state", lambda evidence: None)


async def _load(rows):
    return await merge_review.load_merge_review(
        Session(rows),
        context=_context(),
        node=SimpleNamespace(org_id="org", flow_id="flow"),
        binding=SimpleNamespace(run_id=AUTHOR),
        reviewer_run_id=REVIEWER,
        raw_execution={"current_attempt": {"N": "1"}},
        head_sha=HEAD,
    )


@pytest.mark.asyncio
async def test_newer_ci_snapshot_does_not_hide_the_complete_review_of_the_same_head(monkeypatch):
    _patch(monkeypatch)
    rows = [_row("ci", complete=False, findings=[_ci_finding()]), _row("approve", complete=True, findings=[])]
    evidence = await _load(rows)
    assert evidence.artifact_ref == rows[1].artifact_ref


@pytest.mark.asyncio
async def test_ci_snapshot_without_any_complete_review_still_reads_incomplete(monkeypatch):
    _patch(monkeypatch)
    with pytest.raises(CycleBlockedError) as blocked:
        await _load([_row("ci", complete=False, findings=[_ci_finding()])])
    assert blocked.value.reason == "review_incomplete"


@pytest.mark.asyncio
async def test_a_reviewer_defect_still_blocks_even_with_an_older_approval(monkeypatch):
    _patch(monkeypatch)
    defect = {"finding_id": "null-deref", "severity": "blocking", "disposition": "open", "summary": "crash"}
    with pytest.raises(CycleBlockedError) as blocked:
        await _load([_row("defect", complete=False, findings=[defect]), _row("approve", complete=True, findings=[])])
    assert blocked.value.reason == "review_incomplete"


def test_merged_pr_passes_when_only_the_app_snapshot_requests_changes():
    from src.orchestration.merge_controller import app_snapshot_withdrawn
    from src.orchestration.pr_bindings import MergeEvidence

    review = SimpleNamespace(is_complete_review=True, publication_blockers=(), reviewed_head_sha=HEAD)
    evidence = MergeEvidence(
        merged=True, head_sha=HEAD, checks_successful=True, review_approved=False, review_state="changes_requested", foreign_changes_requested=False
    )
    assert app_snapshot_withdrawn(evidence, review)
    # A human (or any other reviewer) still requesting changes blocks.
    assert not app_snapshot_withdrawn(evidence.__class__(**{**evidence.__dict__, "foreign_changes_requested": True}), review)
    # Provider did not report who requested changes: fail closed.
    assert not app_snapshot_withdrawn(evidence.__class__(**{**evidence.__dict__, "foreign_changes_requested": None}), review)
    # Without the engine's own complete review of the exact merged head: fail closed.
    assert not app_snapshot_withdrawn(evidence, SimpleNamespace(is_complete_review=False, publication_blockers=(), reviewed_head_sha=HEAD))
    assert not app_snapshot_withdrawn(evidence, SimpleNamespace(is_complete_review=True, publication_blockers=(), reviewed_head_sha="0" * 40))
    assert not app_snapshot_withdrawn(evidence.__class__(**{**evidence.__dict__, "merged": False}), review)


def test_own_approval_on_head_identifies_the_reviewer_harness_only_through_its_own_approval():
    from src.orchestration.merge_evidence import own_approval_on_head

    bot = "aws-e-adp-agent-pm"
    assert own_approval_on_head(approved_on_head={bot}, bot_authors={bot}, app_actor="") is True
    assert own_approval_on_head(approved_on_head={bot}, bot_authors=set(), app_actor=bot) is True
    # A human approval, or a bot that approved another head, is not the harness's own approval.
    assert own_approval_on_head(approved_on_head={"alice"}, bot_authors={bot}, app_actor="") is False
    assert own_approval_on_head(approved_on_head=set(), bot_authors={bot}, app_actor=bot) is False


def test_foreign_change_requests_recognises_the_reviewer_bot_by_its_own_approval_of_the_head():
    from src.orchestration.merge_evidence import foreign_change_requests

    bot = "aws-e-adp-agent-pm"
    # The harness approved the head, then recorded its CI snapshot as a change request.
    assert (
        foreign_change_requests(
            {bot: "CHANGES_REQUESTED"},
            approved_on_head={bot},
            bot_authors={bot},
            app_actor="",
            review_decision="CHANGES_REQUESTED",
            app_review_state=None,
        )
        is False
    )
    # A human requesting changes is foreign even when the bot also did.
    assert (
        foreign_change_requests(
            {bot: "CHANGES_REQUESTED", "alice": "CHANGES_REQUESTED"},
            approved_on_head={bot},
            bot_authors={bot},
            app_actor="",
            review_decision="CHANGES_REQUESTED",
            app_review_state=None,
        )
        is True
    )
    # A bot that never approved this head is not the engine's snapshot.
    assert (
        foreign_change_requests(
            {bot: "CHANGES_REQUESTED"},
            approved_on_head=set(),
            bot_authors={bot},
            app_actor="",
            review_decision="CHANGES_REQUESTED",
            app_review_state=None,
        )
        is True
    )
    # A provider decision nobody visible explains stays foreign.
    assert (
        foreign_change_requests(
            {}, approved_on_head=set(), bot_authors=set(), app_actor="", review_decision="CHANGES_REQUESTED", app_review_state=None
        )
        is True
    )
    # The App identified through its verdict comments, or its own comment state, also counts as own.
    assert (
        foreign_change_requests(
            {}, approved_on_head=set(), bot_authors=set(), app_actor=bot, review_decision="CHANGES_REQUESTED", app_review_state="changes_requested"
        )
        is False
    )
    assert foreign_change_requests({}, approved_on_head=set(), bot_authors=set(), app_actor="", review_decision=None, app_review_state=None) is False
