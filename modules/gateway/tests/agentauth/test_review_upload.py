"""The join between an uploaded review result and recorded evidence (#5146).

`test_artifact_service.py` covers the transport around this — the persona 404, the
422s, the receipt, the stored bytes. What it cannot cover is what happens *inside*
the session, because there the observer is stubbed at its own boundary. These tests
drive :func:`observe_review_upload` directly with a fake session and a stubbed
`ingest_review_result`, because the two properties that matter here are orderings
rather than values:

* the credential is re-verified **before** the commit, not after, and
* nothing is committed on any refusal.

Both are invisible to a test that only inspects the response.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from src.agentauth.review_upload import ReviewUploadRefusedError, observe_review_upload
from src.orchestration.review_evidence import ReviewEvidenceRefusal
from tests.agentauth.test_run_services import RECORD

EXECUTION = {
    "orchestration_node_id": {"S": "node-one"},
    "orchestration_node_attempt": {"N": "3"},
    "installation_id": {"N": "4242"},
    "persona": {"S": "reviewer"},
}
DOCUMENT = {"name": "orchestration-review", "version": "v1", "result_id": "r-1"}


class Session:
    """A session that records the order of everything done to it."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def commit(self):
        self.events.append("commit")


@pytest.fixture
def observer(monkeypatch):
    """`observe_review_upload` with its two dependencies replaced.

    `ingest_review_result` is verified against real PostgreSQL in
    `tests/orchestration/test_review_ingest_postgres.py`; here it is a stub so the
    outcome can be steered to each branch. The session factory is replaced because
    this module's contract is *when* it commits, which needs no database.
    """
    events: list[str] = []
    evidence = SimpleNamespace(artifact_ref="artifact:runs/x/review.json")
    outcome = SimpleNamespace(refusal=None, detail=None, recorded=True, ledger=None, evidence=evidence)
    calls: list[dict] = []

    async def ingest(session, **kwargs):
        calls.append(kwargs)
        events.append("ingest")
        return outcome

    monkeypatch.setattr("src.orchestration.review_ingest.ingest_review_result", ingest)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: lambda: Session(events))

    async def reverify():
        events.append("reverify")

    return SimpleNamespace(events=events, outcome=outcome, calls=calls, reverify=reverify)


class TestWhatTheReviewIsCheckedAgainst:
    async def test_every_protected_value_comes_from_server_state(self, observer):
        """The document is the only caller-supplied argument.

        The story and attempt come from the DynamoDB row the gateway wrote at
        dispatch, the reviewer identity from the authenticated credential, and the
        prefix from the same record's server-derived namespace. If any of these were
        taken from the document, a reviewer could nominate the story it reviewed, the
        attempt it reviewed, who it was, or whose artifacts count as its own.
        """
        await observe_review_upload(RECORD, EXECUTION, document=DOCUMENT, reverify=observer.reverify)

        assert observer.calls[0] == {
            "document": DOCUMENT,
            "org_id": "tenant-one",
            "node_id": "node-one",
            "attempt": 3,
            "reviewer_run_id": "run-one",
            "installation_id": 4242,
            "own_artifact_prefix": f"runs/{hashlib.sha256(b'tenant-one').hexdigest()}/{hashlib.sha256(b'run-one').hexdigest()}/attempt-1/",
        }

    async def test_a_document_naming_its_own_protected_values_is_ignored(self, observer):
        """The hostile case the previous test only describes.

        A review result is an untrusted document. If it could carry its own
        `reviewer_run_id`, `node_id`, `attempt` or artifact prefix and have any of
        them preferred — or merely used as a fallback — a reviewer could attribute
        its verdict to another run, point it at a different story or attempt, or
        nominate another run's artifacts as its own evidence. Each value is asserted
        to be the server's even though the document supplies a different one.
        """
        hostile = {
            **DOCUMENT,
            "reviewer_run_id": "run-someone-else",
            "org_id": "tenant-two",
            "node_id": "node-two",
            "attempt": 99,
            "installation_id": 1,
            "artifact_prefix": "runs/attacker/",
            "own_artifact_prefix": "runs/attacker/",
        }
        await observe_review_upload(RECORD, EXECUTION, document=hostile, reverify=observer.reverify)

        call = observer.calls[0]
        assert call["reviewer_run_id"] == "run-one"
        assert call["org_id"] == "tenant-one"
        assert call["node_id"] == "node-one"
        assert call["attempt"] == 3
        assert call["installation_id"] == 4242
        assert call["own_artifact_prefix"].startswith("runs/") and "attacker" not in call["own_artifact_prefix"]
        # Passed through unaltered: the validator has to see exactly what was
        # uploaded, including the hostile fields, or its own checks are weakened.
        assert call["document"] == hostile

    @pytest.mark.parametrize(
        "execution",
        [
            {**EXECUTION, "persona": {"S": "developer"}},
            {**EXECUTION, "persona": {"S": ""}},
            {**EXECUTION, "orchestration_node_id": {"S": ""}},
            {**EXECUTION, "orchestration_node_attempt": {"N": "0"}},
            {**EXECUTION, "installation_id": {"N": "0"}},
            {k: v for k, v in EXECUTION.items() if k != "persona"},
            {k: v for k, v in EXECUTION.items() if k != "orchestration_node_id"},
            {},
        ],
    )
    async def test_only_a_dispatched_reviewer_reaches_the_session(self, observer, execution):
        """Refused before a session is opened, let alone a provider read.

        A developer run filing review evidence about its own work is the case this
        exists for. `ingest_review_result` would refuse it as `SELF_REVIEW`, but
        relying on that would mean the transport admits anything and the narrow rule
        lives somewhere else; an attempt with no engine assignment at all has no
        story to be checked against in the first place.
        """
        with pytest.raises((ValueError, KeyError)):
            await observe_review_upload(RECORD, execution, document=DOCUMENT, reverify=observer.reverify)
        assert observer.events == [], "a caller that is not a dispatched reviewer must do no work"


class TestNothingBecomesDurableWithoutARecheckedCredential:
    async def test_the_credential_is_rechecked_immediately_before_the_commit(self, observer):
        """Order, not presence.

        The authority was verified before a provider read and several queries. A
        grant withdrawn in that window must not leave a committed ledger row, so the
        re-check has to be the last thing before durability — a check after the
        commit would only be able to report a write it can no longer undo.
        """
        await observe_review_upload(RECORD, EXECUTION, document=DOCUMENT, reverify=observer.reverify)
        assert observer.events == ["ingest", "reverify", "commit"]

    async def test_a_failed_recheck_leaves_nothing_committed(self, observer):
        async def withdrawn():
            observer.events.append("reverify")
            raise PermissionError("grant withdrawn")

        with pytest.raises(PermissionError):
            await observe_review_upload(RECORD, EXECUTION, document=DOCUMENT, reverify=withdrawn)
        assert observer.events == ["ingest", "reverify"], "the evidence was committed under a withdrawn credential"

    async def test_a_validation_refusal_carries_its_arm_and_commits_nothing(self, observer):
        """The arm is the whole diagnostic value, so it is not collapsed.

        A reviewer has to know whether to fix its document, retry its publication or
        stop, and those are different arms. Refusals are also the one thing the
        caller may learn specifically — it has already authenticated as itself.
        """
        observer.outcome.refusal = ReviewEvidenceRefusal.STALE_HEAD
        observer.outcome.detail = "The review examined a commit that is no longer the head."

        with pytest.raises(ReviewUploadRefusedError) as refused:
            await observe_review_upload(RECORD, EXECUTION, document=DOCUMENT, reverify=observer.reverify)

        assert refused.value.code == ReviewEvidenceRefusal.STALE_HEAD.value
        assert refused.value.detail == "The review examined a commit that is no longer the head."
        assert "commit" not in observer.events
        assert "reverify" not in observer.events, "a refusal needs no credential re-check; there is nothing to make durable"

    async def test_a_valid_review_the_ledger_declined_is_not_reported_as_recorded(self, observer):
        """Validated but unpersisted is a retry, not a success.

        A claim generation that advanced mid-request, or a conflicting settled
        action. Not the reviewer's defect — but a caller told "recorded" would stop
        retrying and the evidence would then exist nowhere at all.
        """
        observer.outcome.recorded = False
        observer.outcome.ledger = SimpleNamespace(reason="claim_superseded")

        with pytest.raises(ReviewUploadRefusedError) as refused:
            await observe_review_upload(RECORD, EXECUTION, document=DOCUMENT, reverify=observer.reverify)

        assert refused.value.code == "not_recorded"
        assert "retry" in refused.value.detail
        assert "commit" not in observer.events


class TestTheReceipt:
    async def test_it_reports_the_reference_the_evidence_was_recorded_under(self, observer):
        """Enough for a reviewer to cite it, and no document content.

        The reference is what a later read resolves; findings prose in a transport
        receipt would be a second, unvalidated copy of the review.
        """
        receipt = await observe_review_upload(RECORD, EXECUTION, document=DOCUMENT, reverify=observer.reverify)
        assert receipt == {"recorded": True, "evidence_ref": "artifact:runs/x/review.json"}

    async def test_the_reference_is_read_from_the_recorded_evidence(self, observer):
        """Not recomputed here. One source, so the two cannot disagree."""
        observer.outcome.evidence = SimpleNamespace(artifact_ref="artifact:runs/other/review.json")
        receipt = await observe_review_upload(RECORD, EXECUTION, document=DOCUMENT, reverify=observer.reverify)
        assert receipt["evidence_ref"] == "artifact:runs/other/review.json"


class TestTheRefusalIsUsableByAnOperator:
    async def test_the_arm_is_a_contract_value_not_prose(self, observer):
        """Every arm must round-trip to the shared enum.

        A hand-written string here would be a code the reviewer reports and no
        operator runbook can look up.
        """
        for arm in ReviewEvidenceRefusal:
            observer.outcome.refusal = arm
            observer.outcome.detail = "detail"
            with pytest.raises(ReviewUploadRefusedError) as refused:
                await observe_review_upload(RECORD, EXECUTION, document=DOCUMENT, reverify=observer.reverify)
            assert ReviewEvidenceRefusal(refused.value.code) is arm
