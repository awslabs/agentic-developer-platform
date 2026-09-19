"""Review evidence validation against protected state (#5146).

The defect these pin: a reviewer run that exits 0 has established nothing. The
recorded failures are all that shape — a run that found a real correctness blocker
and published only a security report, a run whose prose said APPROVE while the pull
request's review list stayed empty, a run whose findings described one commit while
the verdict landed on another. Every one of them *succeeded*.

So the organising principle here is the same one `test_pr_bindings.py` states: **an
absent answer is never a pass.** Each refusal arm is asserted to return its OWN
typed code, because "the reviewer ran and the story did not move" was true,
unactionable, and in several observed cases describing something that could never
resolve on its own.

The documents come from `contracts/orchestration-review/v1/review-result.golden.json`
— the same file the contract suite and the worker producer read. There is
deliberately ONE artifact: two independent fixtures, each asserting its own side's
assumption, is exactly how the provenance drift went unnoticed for months
(`tests/internal/test_provenance_contract.py` says the same thing).

Deliberately NOT asserted anywhere in this file:

- that accepted evidence means the change may merge. Nothing here reads the
  provider's review list, its required checks or its merge state; that is
  `pr_bindings.evidence_for_binding`'s job and this module never re-decides it.
- that a distinct reviewer run satisfies GitHub's independent-approval rule. The
  check is necessary and not sufficient, and `test_distinct_runs_are_not_provider_approval`
  pins that the module says so.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.execution_state import BlockCode, ExecutionIdentity
from src.orchestration.models import (
    BindingRole,
    BindingState,
    NodeKind,
    NodeState,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from src.orchestration.review_evidence import (
    REVIEW_CONTRACT_NAME,
    REVIEW_CONTRACT_VERSION,
    ReviewEvidenceError,
    ReviewEvidenceRefusal,
    evidence_action_intent,
    evidence_decision_snapshot,
    evidence_observation,
    evidence_operation_key,
    evidence_summary,
    legacy_review_note,
    outstanding_block,
    parse_review_result,
    refusal_explanation,
    resolve_expected_subject,
    review_artifact_ref,
    validate_review_result,
)
from src.shared.models.base import Base

# parents[3] is the repository root: orchestration → tests → gateway → modules → root.
_REPO_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_PATH = _REPO_ROOT / "contracts" / "orchestration-review" / "v1" / "review-result.golden.json"

with GOLDEN_PATH.open() as fh:
    GOLDEN = json.load(fh)


def _doc(key: str) -> dict:
    """An accepted golden document, minus its `$comment` documentation keys."""
    return {k: v for k, v in GOLDEN[key].items() if not k.startswith("$")}


APPROVE = _doc("accepted_result_approve")
SECURITY_ONLY = _doc("accepted_result_security_only_incomplete")
REFUSED = _doc("accepted_result_publication_refused")
PUBLICATION_FAILED = _doc("accepted_result_approve_publication_failed")

# Protected values, taken from the golden document so the happy path is a genuine
# match rather than two hand-written constants that happen to agree.
ORG = APPROVE["scope"]["org_id"]
FLOW = APPROVE["scope"]["flow_id"]
NODE = APPROVE["scope"]["node_id"]
CYCLE = APPROVE["scope"]["cycle"]
PLAN_VERSION = APPROVE["authority"]["accepted_plan_version"]
CLAIM = APPROVE["authority"]["claim_id"]
GENERATION = APPROVE["authority"]["claim_generation"]
REPO_ID = APPROVE["repository"]["provider_repository_id"]
REPO = APPROVE["repository"]["repo"]
PR_NUMBER = APPROVE["subject"]["pr_number"]
PR_NODE = APPROVE["subject"]["provider_pr_node_id"]
HEAD = APPROVE["subject"]["reviewed_head_sha"]
AUTHOR_RUN = APPROVE["lineage"]["author_run_id"]
REVIEWER_RUN = APPROVE["lineage"]["reviewer_run_id"]

MOVED_HEAD = "b0f4c9e1d2a37568194ac0e5bb7d2f6318ae4c90"
OTHER_ORG = "org-somebody-else"


def identity(*, cycle: int = CYCLE, plan_version: int = PLAN_VERSION, generation: int = GENERATION) -> ExecutionIdentity:
    """The authority binding the caller resolved from the execution row.

    Built here from protected-state values, never from the submitted document —
    which is the whole property under test.
    """
    return ExecutionIdentity(
        org_id=ORG,
        node_id=NODE,
        cycle=cycle,
        accepted_plan_version=plan_version,
        claim_id=CLAIM,
        claim_generation=generation,
    )


def binding(
    *,
    repo_id: int = REPO_ID,
    repo: str = REPO,
    pr_number: int = PR_NUMBER,
    pr_node: str = PR_NODE,
    head: str = HEAD,
    role: str = BindingRole.IMPLEMENTATION.value,
    state: str = BindingState.ACTIVE.value,
) -> OrchestrationPullRequestBinding:
    """A registered binding, as the server wrote it.

    Detached from any session on purpose: `validate_review_result` compares fields
    and issues no query, and a test that needed a database to check a comparison
    would be asserting the ORM instead of the rule.
    """
    return OrchestrationPullRequestBinding(
        org_id=ORG,
        flow_id=FLOW,
        node_id=NODE,
        attempt=1,
        run_id=AUTHOR_RUN,
        provider_repository_id=repo_id,
        provider_pr_node_id=pr_node,
        repo=repo,
        pr_number=pr_number,
        installation_id=4242,
        head_sha=head,
        revision=1,
        role=role,
        state=state,
        registered_by=AUTHOR_RUN,
        registered_by_kind="service",
    )


def matching_state(document: dict) -> dict:
    """The protected state a server would hold for a document that IS legitimate.

    Derived from the document so that a test about some *other* field is not
    accidentally also a scope-mismatch test. It does not weaken any assertion: every
    forgery test overrides one section and asserts the arm for that section, and the
    happy path is the case where the server's own state genuinely agrees.
    """
    return {
        "identity": ExecutionIdentity(
            org_id=document["scope"]["org_id"],
            node_id=document["scope"]["node_id"],
            cycle=document["scope"]["cycle"],
            accepted_plan_version=document["authority"]["accepted_plan_version"],
            claim_id=document["authority"]["claim_id"],
            claim_generation=document["authority"]["claim_generation"],
        ),
        "binding": binding(
            repo_id=document["repository"]["provider_repository_id"],
            pr_number=document["subject"]["pr_number"],
            pr_node=document["subject"]["provider_pr_node_id"],
            head=document["subject"]["reviewed_head_sha"],
        ),
        "flow_id": document["scope"]["flow_id"],
        "author_run_id": document["lineage"]["author_run_id"],
    }


#: Accepted goldens other than APPROVE, which are validated against state derived
#: from themselves. Built from the fixture rather than hand-listed so a document
#: added to the shared artifact cannot silently be checked against APPROVE's state.
_OWN_STATE_RESULT_IDS = {_doc(key)["result_id"] for key in GOLDEN if key.startswith("accepted_") and key != "accepted_result_approve"}


def accept(document: dict | None = None, **overrides):
    """Validate a document against protected state, with the happy path as default.

    The default state is the state that matches APPROVE. Passing another accepted
    golden document supplies the state matching *it*, so a caller testing publication
    behaviour does not first have to restate five unrelated identifiers.
    """
    body = document if document is not None else APPROVE
    # Keyed on `result_id`, which `patched()` never touches: a forged variant of
    # APPROVE is checked against APPROVE's state, so the one patched section is the
    # only thing that can differ. Another accepted golden gets its own state.
    reference = body if body.get("result_id") in _OWN_STATE_RESULT_IDS else APPROVE
    base = matching_state(reference)
    base.update(overrides)
    return validate_review_result(body, **base)


def refusal(document: dict | None = None, **overrides) -> ReviewEvidenceRefusal:
    """The code a refusal fired with. Fails the test if the document was accepted."""
    with pytest.raises(ReviewEvidenceError) as caught:
        accept(document, **overrides)
    return caught.value.code


def patched(base: dict, **sections) -> dict:
    """A golden document with whole sections replaced, for forging one field at a time."""
    body = dict(base)
    for key, value in sections.items():
        body[key] = {**body[key], **value} if isinstance(value, dict) and isinstance(body.get(key), dict) else value
    return body


# ---------------------------------------------------------------------------
# The shared artifact
# ---------------------------------------------------------------------------


class TestOneSharedArtifact:
    """The consumer reads the producer's fixture, not a copy of it.

    If this file grew its own inline documents, the two sides could drift and both
    suites would stay green — the #4029 failure this pattern exists to prevent.
    """

    def test_golden_fixture_is_the_shared_one(self):
        assert GOLDEN_PATH.is_file(), f"shared review-result fixture not found at {GOLDEN_PATH}"

    def test_consumer_reads_every_accepted_document(self):
        for key in (key for key in GOLDEN if key.startswith("accepted_")):
            parse_review_result(_doc(key))

    def test_contract_pin_matches_the_fixture(self):
        assert APPROVE["name"] == REVIEW_CONTRACT_NAME
        assert APPROVE["version"] == REVIEW_CONTRACT_VERSION


class TestMalformedAndWrongContract:
    def test_another_contract_is_refused_as_wrong_contract(self):
        """Not `MALFORMED_RESULT`: a v2 producer is a coordination problem, not a bug."""
        body = dict(APPROVE)
        body["version"] = 2
        with pytest.raises(ReviewEvidenceError) as caught:
            parse_review_result(body)
        assert caught.value.code is ReviewEvidenceRefusal.WRONG_CONTRACT

    def test_foreign_document_is_refused(self):
        with pytest.raises(ReviewEvidenceError) as caught:
            parse_review_result({"name": "hitl-ticket", "version": 1})
        assert caught.value.code is ReviewEvidenceRefusal.WRONG_CONTRACT

    def test_malformed_result_is_refused_not_partially_accepted(self):
        body = dict(APPROVE)
        body["subject"] = {**body["subject"], "reviewed_head_sha": "abc123"}
        with pytest.raises(ReviewEvidenceError) as caught:
            parse_review_result(body)
        assert caught.value.code is ReviewEvidenceRefusal.MALFORMED_RESULT

    def test_non_object_payload_is_refused(self):
        with pytest.raises(ReviewEvidenceError) as caught:
            parse_review_result(["not", "an", "object"])  # type: ignore[arg-type]
        assert caught.value.code is ReviewEvidenceRefusal.MALFORMED_RESULT

    def test_security_only_shape_never_reaches_the_consumer(self):
        """The contract rejects it, so the consumer cannot be the layer that lets it in."""
        body = dict(APPROVE)
        body["stages"] = [{"name": "security", "outcome": "completed"}]
        assert refusal(body) is ReviewEvidenceRefusal.MALFORMED_RESULT


# ---------------------------------------------------------------------------
# Each mutation gets its own code
# ---------------------------------------------------------------------------


class TestEveryRefusalArmIsDistinct:
    """One code per failure mode. The original failures were expensive to diagnose
    precisely because the answer was generic.
    """

    def test_wrong_tenant(self):
        body = patched(APPROVE, scope={"org_id": OTHER_ORG})
        assert refusal(body) is ReviewEvidenceRefusal.TENANT_MISMATCH

    def test_wrong_node(self):
        body = patched(APPROVE, scope={"node_id": "some-other-node"})
        assert refusal(body) is ReviewEvidenceRefusal.SCOPE_MISMATCH

    def test_wrong_cycle(self):
        """A review of cycle 1 is not evidence about the repair in cycle 2."""
        body = patched(APPROVE, scope={"cycle": CYCLE + 1})
        assert refusal(body) is ReviewEvidenceRefusal.SCOPE_MISMATCH

    def test_wrong_flow(self):
        body = patched(APPROVE, scope={"flow_id": "11111111-2222-3333-4444-555555555555"})
        assert refusal(body) is ReviewEvidenceRefusal.SCOPE_MISMATCH

    def test_wrong_accepted_plan_version(self):
        body = patched(APPROVE, authority={"accepted_plan_version": PLAN_VERSION + 1})
        assert refusal(body) is ReviewEvidenceRefusal.POLICY_MISMATCH

    def test_stale_claim_generation(self):
        """The ownership fence. A superseded run's evidence must not advance the
        current owner's delivery.
        """
        body = patched(APPROVE, authority={"claim_generation": GENERATION - 1})
        assert refusal(body) is ReviewEvidenceRefusal.STALE_CLAIM

    def test_wrong_claim_id(self):
        body = patched(APPROVE, authority={"claim_id": "claim-belonging-to-other-work"})
        assert refusal(body) is ReviewEvidenceRefusal.STALE_CLAIM

    def test_wrong_repository(self):
        body = patched(APPROVE, repository={"provider_repository_id": REPO_ID + 1})
        assert refusal(body) is ReviewEvidenceRefusal.REPOSITORY_MISMATCH

    def test_repository_is_compared_on_the_immutable_id(self):
        """A rename re-points `repo`; it must not re-point the association.

        The document's display name is deliberately stale here while the id matches.
        Accepting it is correct — the id is the identity — and the assertion exists so
        a future edit cannot quietly start authorizing off the mutable name.
        """
        body = patched(APPROVE, repository={"repo": "aws-e/adp-renamed"})
        evidence = accept(body)
        assert evidence.repo == REPO, "the binding's name is authoritative for display"

    def test_wrong_pull_request_number(self):
        body = patched(APPROVE, subject={"pr_number": PR_NUMBER + 7})
        assert refusal(body) is ReviewEvidenceRefusal.PR_MISMATCH

    def test_wrong_pull_request_node_id(self):
        body = patched(APPROVE, subject={"provider_pr_node_id": "PR_kwDOsomethingelse"})
        assert refusal(body) is ReviewEvidenceRefusal.PR_MISMATCH

    def test_moved_head(self):
        assert refusal(binding=binding(head=MOVED_HEAD)) is ReviewEvidenceRefusal.STALE_HEAD

    def test_forged_author_lineage(self):
        """A submitted author id is a claim; the dispatch record is the fact.

        Without this arm a reviewer could defeat the self-review check by naming some
        run it is not as the author.
        """
        body = patched(APPROVE, lineage={"author_run_id": "run-i-made-up"})
        assert refusal(body) is ReviewEvidenceRefusal.SELF_REVIEW

    def test_self_review(self):
        body = patched(APPROVE, lineage={"author_run_id": REVIEWER_RUN, "reviewer_run_id": REVIEWER_RUN})
        assert refusal(body, author_run_id=REVIEWER_RUN) is ReviewEvidenceRefusal.MALFORMED_RESULT

    def test_self_review_caught_against_protected_state(self):
        """The contract catches an equal pair inside one document. This arm catches the
        case the contract structurally cannot: a document whose two run ids differ,
        submitted for work whose real author is the reviewer.
        """
        body = patched(APPROVE, lineage={"author_run_id": "run-a-decoy", "reviewer_run_id": REVIEWER_RUN})
        assert refusal(body, author_run_id=REVIEWER_RUN) is ReviewEvidenceRefusal.SELF_REVIEW

    def test_every_arm_produced_a_distinct_code(self):
        """Guards against a future refactor collapsing arms into one generic code."""
        codes = {
            refusal(patched(APPROVE, scope={"org_id": OTHER_ORG})),
            refusal(patched(APPROVE, scope={"node_id": "other"})),
            refusal(patched(APPROVE, authority={"accepted_plan_version": PLAN_VERSION + 1})),
            refusal(patched(APPROVE, authority={"claim_generation": GENERATION - 1})),
            refusal(patched(APPROVE, repository={"provider_repository_id": REPO_ID + 1})),
            refusal(patched(APPROVE, subject={"pr_number": PR_NUMBER + 7})),
            refusal(binding=binding(head=MOVED_HEAD)),
            refusal(patched(APPROVE, lineage={"author_run_id": "run-i-made-up"})),
        }
        assert len(codes) == 8, f"refusal arms collapsed into shared codes: {sorted(codes)}"


class TestTenantIsolationIsCheckedFirst:
    def test_cross_tenant_document_is_not_diffed_against_this_tenants_state(self):
        """A foreign document must refuse on tenancy, not leak which other field
        also happened to differ from this tenant's protected values.
        """
        body = patched(
            APPROVE,
            scope={"org_id": OTHER_ORG, "node_id": "their-node"},
            subject={"pr_number": 999_999},
        )
        assert refusal(body) is ReviewEvidenceRefusal.TENANT_MISMATCH


# ---------------------------------------------------------------------------
# Stale head
# ---------------------------------------------------------------------------


class TestStaleHead:
    """A review of code that is no longer there is not evidence about what is."""

    def test_provider_head_overrides_the_binding_when_supplied(self):
        """The binding and the document can agree and both be out of date: the head
        moved after registration. The provider's current head is what matters.
        """
        assert refusal(actual_head_sha=MOVED_HEAD) is ReviewEvidenceRefusal.STALE_HEAD

    def test_matching_provider_head_is_accepted(self):
        assert accept(actual_head_sha=HEAD).reviewed_head_sha == HEAD

    def test_refusal_names_both_commits(self):
        with pytest.raises(ReviewEvidenceError) as caught:
            accept(actual_head_sha=MOVED_HEAD)
        assert HEAD in caught.value.message and MOVED_HEAD in caught.value.message

    def test_stale_head_is_refused_not_downgraded_into_evidence(self):
        """`invalidate_for_head` exists for a caller retaining the downgraded artifact.
        As evidence *for the current revision*, a review of other code is simply not
        it, so there is no accepted-but-stale result.
        """
        with pytest.raises(ReviewEvidenceError):
            accept(actual_head_sha=MOVED_HEAD)


# ---------------------------------------------------------------------------
# Accepted is not approved
# ---------------------------------------------------------------------------


class TestAcceptedIsNotApproved:
    def test_valid_bound_result_is_accepted(self):
        evidence = accept()
        assert evidence.pr_number == PR_NUMBER
        assert evidence.reviewed_head_sha == HEAD
        assert evidence.approval_blockers == ()
        assert evidence.is_complete_review is True

    def test_incomplete_review_is_accepted_and_carries_its_reasons(self):
        """Recording an incomplete review must succeed. Refusing to record it would
        destroy the artifact an operator needs, and the observed runs are exactly
        this shape: complete process, incomplete review.
        """
        evidence = accept(SECURITY_ONLY)
        assert evidence.is_complete_review is False
        assert any("functional" in reason for reason in evidence.approval_blockers)

    def test_unpublished_verdict_is_accepted_and_blocks(self):
        """The central observation: stages ran, prose was posted, the review list
        stayed empty. That must be recordable AND must not read as a review.
        """
        evidence = accept(REFUSED)
        assert evidence.is_complete_review is False
        assert any("not published" in reason for reason in evidence.approval_blockers)

    def test_blockers_are_reasons_not_a_boolean(self):
        evidence = accept(REFUSED)
        assert isinstance(evidence.approval_blockers, tuple)
        assert all(isinstance(reason, str) and reason for reason in evidence.approval_blockers)

    def test_complete_review_is_not_named_approved(self):
        """A naming assertion, deliberately. `may_merge` or `approved` on this object
        is how the next caller skips the provider-side checks it cannot see.
        """
        from src.orchestration import review_evidence as module

        exported = set(module.__all__) | set(dir(module.ReviewEvidence))
        assert not {"may_merge", "approved", "is_approved"} & exported

    def test_distinct_runs_are_not_provider_approval(self):
        """The module must say the reviewer/author check is not sufficient, because a
        reader who sees it enforced may assume independent approval is discharged.
        """
        text = refusal_explanation(ReviewEvidenceRefusal.SELF_REVIEW)
        assert "independent" in text


# ---------------------------------------------------------------------------
# Artifact trust
# ---------------------------------------------------------------------------


class TestArtifactTrust:
    def test_unverifiable_head_bound_artifact_is_refused(self):
        assert refusal(trusted_artifact_refs=frozenset()) is ReviewEvidenceRefusal.UNTRUSTED_ARTIFACT

    def test_verified_artifacts_are_accepted(self):
        trusted = frozenset(ref["ref"] for ref in APPROVE["evidence_refs"])
        assert accept(trusted_artifact_refs=trusted).is_complete_review is True

    def test_not_checking_is_distinguishable_from_checking(self):
        """`None` means the caller states it did not verify; an empty frozenset means
        it verified nothing. Collapsing them would make 'not checked' silently pass
        as 'checked and trusted'.
        """
        assert accept(trusted_artifact_refs=None).is_complete_review is True
        with pytest.raises(ReviewEvidenceError):
            accept(trusted_artifact_refs=frozenset())

    def test_finding_level_evidence_is_verified_too(self):
        """A 'resolved' blocking finding's proof lives on the finding. An
        unverifiable proof of a fix is the false-negative shape the issue records.
        """
        body = dict(APPROVE)
        body["findings"] = [
            {
                "finding_id": "blocker-1",
                "stage": "functional",
                "severity": "blocking",
                "disposition": "resolved",
                "summary": "A blocking defect, re-tested at this head.",
                "evidence_refs": [{"kind": "test-run", "ref": "check-run:unverifiable", "head_bound": True}],
            }
        ]
        trusted = frozenset(ref["ref"] for ref in APPROVE["evidence_refs"])
        assert refusal(body, trusted_artifact_refs=trusted) is ReviewEvidenceRefusal.UNTRUSTED_ARTIFACT

    def test_static_artifact_needs_no_head_binding(self):
        """A contract file is not invalidated by a commit, so it is not head-bound and
        must not be demanded in the trusted set.
        """
        head_bound = frozenset(ref["ref"] for ref in APPROVE["evidence_refs"] if ref["head_bound"])
        assert accept(trusted_artifact_refs=head_bound).is_complete_review is True


# ---------------------------------------------------------------------------
# Expected-PR resolution against real rows
# ---------------------------------------------------------------------------


@pytest.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _disable_pysqlite_implicit_begin(dbapi_connection, _record):
        dbapi_connection.isolation_level = None

    @event.listens_for(eng.sync_engine, "begin")
    def _emit_explicit_begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine):
    async with async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)() as s:
        yield s


async def _story(session: AsyncSession) -> tuple[str, str]:
    """A flow and story node the binding can hang off. Returns (flow_id, node_id).

    The ids are the golden document's own, so the end-to-end case is a legitimate
    submission rather than one that has to be excused. The golden fixture uses real
    UUID-shaped values precisely so it can be used this way.
    """
    flow = OrchestrationFlow(id=FLOW, org_id=ORG, slug="flow-5146", title="Deliver the epic", state="running")
    session.add(flow)
    await session.flush()
    node = OrchestrationNode(
        id=NODE,
        org_id=ORG,
        flow_id=flow.id,
        epic_ref="epic-1",
        wave_ref="wave-1",
        node_ref="story-5146",
        kind=NodeKind.STORY.value,
        state=NodeState.AWAITING_MERGE.value,
        title="Implement the thing",
        issue_ref="5146",
        attempts=1,
    )
    session.add(node)
    await session.flush()
    return flow.id, node.id


async def _add_binding(session: AsyncSession, flow_id: str, node_id: str, **overrides) -> OrchestrationPullRequestBinding:
    row = binding(**overrides)
    row.flow_id = flow_id
    row.node_id = node_id
    session.add(row)
    await session.flush()
    return row


class TestExpectedSubjectComesFromProtectedState:
    """Which pull request was expected is read from the server's own binding, never
    from the submitted result. The observed wrong-revision run needed exactly this:
    it analysed one commit and had the verdict attached to another, and nothing
    compared the two.
    """

    async def test_active_implementation_binding_is_returned(self, session):
        flow_id, node_id = await _story(session)
        await _add_binding(session, flow_id, node_id)
        resolved = await resolve_expected_subject(session, org_id=ORG, node_id=node_id, attempt=1)
        assert resolved.pr_number == PR_NUMBER
        assert resolved.head_sha == HEAD

    async def test_no_binding_is_refused_not_defaulted(self, session):
        flow_id, node_id = await _story(session)
        with pytest.raises(ReviewEvidenceError) as caught:
            await resolve_expected_subject(session, org_id=ORG, node_id=node_id, attempt=1)
        assert caught.value.code is ReviewEvidenceRefusal.NO_BINDING

    async def test_ambiguous_candidates_are_refused_not_guessed(self, session):
        """Two active bindings mean the association is genuinely unknown. Picking the
        newest would accept review evidence for an arbitrary pull request.
        """
        flow_id, node_id = await _story(session)
        await _add_binding(session, flow_id, node_id)
        await _add_binding(session, flow_id, node_id, pr_number=PR_NUMBER + 1, pr_node="PR_kwDOsecond")
        with pytest.raises(ReviewEvidenceError) as caught:
            await resolve_expected_subject(session, org_id=ORG, node_id=node_id, attempt=1)
        assert caught.value.code is ReviewEvidenceRefusal.AMBIGUOUS_PR

    async def test_reviewer_artifact_binding_is_refused(self, session):
        """The recursion shape: a reviewer run pushes its transcript to the same issue's
        branch family, so a review *of that PR* is not evidence about the delivery.
        """
        flow_id, node_id = await _story(session)
        await _add_binding(session, flow_id, node_id, role=BindingRole.REVIEWER_ARTIFACT.value)
        with pytest.raises(ReviewEvidenceError) as caught:
            await resolve_expected_subject(session, org_id=ORG, node_id=node_id, attempt=1)
        assert caught.value.code is ReviewEvidenceRefusal.NOT_IMPLEMENTATION

    async def test_another_tenants_binding_is_not_visible(self, session):
        flow_id, node_id = await _story(session)
        await _add_binding(session, flow_id, node_id)
        with pytest.raises(ReviewEvidenceError) as caught:
            await resolve_expected_subject(session, org_id=OTHER_ORG, node_id=node_id, attempt=1)
        assert caught.value.code is ReviewEvidenceRefusal.NO_BINDING

    async def test_superseded_binding_is_refused_by_the_validator(self, session):
        """`active_binding_for_node` filters superseded rows out, so resolution reports
        their absence as `NO_BINDING`. The validator holds the arm for a caller that
        resolved a binding some other way — a review of superseded work is not
        evidence about the current work.
        """
        assert refusal(binding=binding(state=BindingState.SUPERSEDED.value)) is ReviewEvidenceRefusal.SUPERSEDED_BINDING

    async def test_superseded_row_is_absent_from_resolution(self, session):
        flow_id, node_id = await _story(session)
        await _add_binding(session, flow_id, node_id, state=BindingState.SUPERSEDED.value)
        with pytest.raises(ReviewEvidenceError) as caught:
            await resolve_expected_subject(session, org_id=ORG, node_id=node_id, attempt=1)
        assert caught.value.code is ReviewEvidenceRefusal.NO_BINDING

    async def test_superseded_attempt_does_not_answer_for_the_current_one(self, session):
        """A binding registered under attempt 1 must not be the expected subject for
        attempt 2's review.
        """
        flow_id, node_id = await _story(session)
        await _add_binding(session, flow_id, node_id)
        with pytest.raises(ReviewEvidenceError) as caught:
            await resolve_expected_subject(session, org_id=ORG, node_id=node_id, attempt=2)
        assert caught.value.code is ReviewEvidenceRefusal.NO_BINDING

    async def test_end_to_end_resolution_then_validation(self, session):
        """The real call shape: resolve from protected state, then validate against it.

        Nothing in this path consults the document for which pull request to expect.
        """
        flow_id, node_id = await _story(session)
        await _add_binding(session, flow_id, node_id)
        resolved = await resolve_expected_subject(session, org_id=ORG, node_id=node_id, attempt=1)
        evidence = validate_review_result(
            APPROVE,
            identity=ExecutionIdentity(
                org_id=ORG,
                node_id=node_id,
                cycle=CYCLE,
                accepted_plan_version=PLAN_VERSION,
                claim_id=CLAIM,
                claim_generation=GENERATION,
            ),
            binding=resolved,
            flow_id=flow_id,
            author_run_id=AUTHOR_RUN,
        )
        # The reviewed commit is named in the reference, but is no longer the last
        # component: the result identity is appended so two different results at one
        # head do not share a reference. See TestEvidenceIdentityIsPerResult.
        assert HEAD in evidence.artifact_ref


# ---------------------------------------------------------------------------
# Persistence: references only, through the existing ledger
# ---------------------------------------------------------------------------


class TestPersistenceCarriesReferencesOnly:
    def test_operation_key_is_derived_from_the_work(self):
        """A per-attempt key would satisfy the type and defeat the protection: every
        retry would look like a second review.
        """
        first, second = accept(), accept()
        assert evidence_operation_key(first) == evidence_operation_key(second)

    def test_a_review_of_a_different_commit_is_different_evidence(self):
        moved = patched(APPROVE, subject={"reviewed_head_sha": MOVED_HEAD}, publication={"published_head_sha": MOVED_HEAD})
        other = accept(moved, binding=binding(head=MOVED_HEAD))
        assert evidence_operation_key(other) != evidence_operation_key(accept())

    def test_artifact_ref_is_a_reference_not_a_document(self):
        ref = review_artifact_ref(parse_review_result(APPROVE))
        assert ref.startswith("review:")
        assert len(ref) < 200, "an artifact ref is a pointer, not a payload"

    def test_action_intent_detail_is_small_and_non_sensitive(self):
        intent = evidence_action_intent(accept())
        assert intent.kind == "review_evidence"
        assert intent.artifact_ref
        # An exact set, not a subset: this row is operator-readable and surfaced in
        # diagnostics, so a new key must be a deliberate decision that it is safe to
        # persist. `publication_outstanding` is a boolean about the artifact, derived
        # from the publication outcome already present on the line above.
        assert set(intent.detail) == {
            "repo",
            "pr_number",
            "reviewed_head_sha",
            "verdict",
            "publication",
            "complete_review",
            "publication_outstanding",
        }
        assert all(isinstance(value, str) for value in intent.detail.values())

    def test_incomplete_review_is_a_recorded_success_not_a_failure(self):
        """Flattening 'incomplete review' into FAILED would lose the artifact. What
        succeeded is the recording; what is incomplete is stated in the detail.
        """
        evidence = accept(REFUSED)
        observation = evidence_observation(evidence)
        assert observation.outcome.value == "succeeded"
        assert "not published" in (observation.detail or "")

    def test_observation_detail_is_bounded(self):
        """An operator column is not a transcript store."""
        body = dict(APPROVE)
        body["verdict"] = "request-changes"
        body["findings"] = [
            {
                "finding_id": f"blocker-{index}",
                "stage": "functional",
                "severity": "blocking",
                "disposition": "open",
                "summary": "x" * 400,
                "evidence_refs": [],
            }
            for index in range(20)
        ]
        evidence = accept(body)
        assert len(evidence_observation(evidence).detail or "") <= 900

    def test_observation_settles_the_same_operation_key(self):
        evidence = accept()
        assert evidence_observation(evidence).operation_key == evidence_action_intent(evidence).operation_key

    async def test_recorded_through_the_ledger_with_no_new_schema(self, session):
        """Persistence is one prepared action plus one observation on the existing
        ledger. A synthetic store stands in for PostgreSQL; what is asserted is the
        call shape, not the store's own semantics (`test_execution_store.py` owns those).
        """
        from src.orchestration import review_evidence as module
        from tests.orchestration.execution_ledger_fixtures import SyntheticExecutionStore

        store = SyntheticExecutionStore()
        ident = identity()
        await store.create_execution(identity=ident, flow_id=FLOW)
        module_prepare, module_observe = store.prepare_action, store.record_observation
        import src.orchestration.execution_store as real_store

        original = (real_store.prepare_action, real_store.record_observation)
        real_store.prepare_action, real_store.record_observation = module_prepare, module_observe
        try:
            outcome = await module.record_review_evidence(session, identity=ident, evidence=accept())
        finally:
            real_store.prepare_action, real_store.record_observation = original
        assert outcome.kind.value == "applied"
        assert outcome.action is not None
        assert outcome.action.artifact_ref == accept().artifact_ref

    def test_decision_snapshot_is_stable_and_reference_only(self):
        evidence = accept()
        now = datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC)
        first = evidence_decision_snapshot(evidence, now=now)
        assert first == evidence_decision_snapshot(evidence, now=now)
        body = json.loads(first)
        assert body["artifact_ref"] == evidence.artifact_ref
        assert "summary" not in json.dumps(body), "finding prose belongs with the artifact, not the snapshot"


# ---------------------------------------------------------------------------
# Operator-facing surfaces
# ---------------------------------------------------------------------------


class TestRefusalsExplainThemselves:
    @pytest.mark.parametrize("code", list(ReviewEvidenceRefusal))
    def test_every_refusal_has_operator_prose(self, code: ReviewEvidenceRefusal):
        """A held story must explain itself. The observed failures were unactionable
        precisely because the reason was generic.
        """
        text = refusal_explanation(code)
        assert len(text) > 40, f"{code} has no usable explanation"
        assert code.value not in text, "the explanation must be prose, not the code echoed back"

    @pytest.mark.parametrize("code", list(ReviewEvidenceRefusal))
    def test_every_refusal_maps_to_an_owned_block(self, code: ReviewEvidenceRefusal):
        block = outstanding_block(code, owner="platform-operator")
        assert block.owner == "platform-operator"
        assert block.required_input
        assert code.value in (block.detail or "")

    def test_authority_arms_are_not_routed_to_a_human_gate(self):
        """A stale claim generation is not a question an operator answers; the current
        owner reviewing again resolves it. Mis-routing it would park work on a human.
        """
        assert outstanding_block(ReviewEvidenceRefusal.STALE_CLAIM, owner="engine").code is BlockCode.AUTHORITY_UNVERIFIABLE

    def test_ambiguous_binding_routes_to_a_human(self):
        assert outstanding_block(ReviewEvidenceRefusal.AMBIGUOUS_PR, owner="operator").code is BlockCode.HUMAN_INPUT_REQUIRED

    def test_contract_unavailable_is_a_platform_condition(self):
        assert outstanding_block(ReviewEvidenceRefusal.CONTRACT_UNAVAILABLE, owner="platform").code is BlockCode.PROVIDER_UNAVAILABLE

    def test_no_block_code_is_an_unowned_stall(self):
        for code in ReviewEvidenceRefusal:
            assert outstanding_block(code, owner="x").owner


class TestReadSurface:
    def test_summary_excludes_finding_prose_and_internal_refs(self):
        body = evidence_summary(accept())
        assert body["pr_number"] == PR_NUMBER
        assert body["reviewed_head_sha"] == HEAD
        assert "evidence_refs" not in body
        assert "findings" not in body

    def test_summary_reports_blockers_rather_than_a_bare_boolean(self):
        body = evidence_summary(accept())
        assert isinstance(body["approval_blockers"], list)
        assert body["complete_review"] is True


# ---------------------------------------------------------------------------
# Legacy output
# ---------------------------------------------------------------------------


class TestLegacyOutputStaysParseableAndNonAuthoritative:
    """Existing reviewer prose must keep working and must never become approval.
    The whole defect was prose being read as a conclusion.
    """

    def test_legacy_prose_still_parses(self):
        note = legacy_review_note("Functional review: APPROVE. All six issues fixed.")
        assert note["kind"] == "legacy_review_output"
        assert "APPROVE" in note["text"]

    def test_legacy_prose_grants_nothing(self):
        note = legacy_review_note("Functional review: APPROVE.")
        assert note["authoritative"] is False
        assert note["grants_approval"] is False
        assert "verdict" not in note

    def test_legacy_note_is_bounded(self):
        assert len(legacy_review_note("x" * 10_000)["text"]) <= 2000

    def test_empty_legacy_output_is_not_an_error(self):
        assert legacy_review_note("")["text"] == ""

    def test_there_is_no_path_from_prose_to_evidence(self):
        """A compatibility shim that produced `ReviewEvidence` from text would
        reintroduce the defect with a contract's blessing.
        """
        note = legacy_review_note("APPROVE")
        assert not isinstance(note, type(accept()))
        assert "artifact_ref" not in note


# ---------------------------------------------------------------------------
# Producer attribution: the authenticated producer, not the document's claim
# ---------------------------------------------------------------------------


class TestReviewerIsBoundToTheAuthenticatedProducer:
    """`reviewer != author` is necessary and not sufficient.

    Reproduced finding: an arbitrary substituted `reviewer_run_id` validated with
    `is_complete_review=True`, because the only lineage comparison was against the
    author. That means the run credited with a review need not be the run that
    performed it — so the "independently attributable reviewer run" this contract
    exists to establish was not established.
    """

    def test_substituted_reviewer_is_refused_against_the_authenticated_producer(self):
        body = patched(APPROVE, lineage={"reviewer_run_id": "run-somebody-else-entirely"})
        assert refusal(body, reviewer_run_id=REVIEWER_RUN) is ReviewEvidenceRefusal.REVIEWER_MISMATCH

    def test_matching_reviewer_is_accepted(self):
        assert accept(reviewer_run_id=REVIEWER_RUN).is_complete_review

    def test_substitution_passes_the_self_review_check_it_is_not_caught_by(self):
        """Pin *why* the separate arm is needed.

        The substituted id differs from the author, so the self-review check is
        satisfied and cannot be the thing that refuses this. Without an
        authenticated producer to compare against, the document is accepted.
        """
        body = patched(APPROVE, lineage={"reviewer_run_id": "run-somebody-else-entirely"})
        assert body["lineage"]["reviewer_run_id"] != AUTHOR_RUN
        assert accept(body).is_complete_review

    def test_unverified_producer_is_not_silently_trusted(self):
        """Omitting the producer must not read as "verified".

        `None` means the caller is stating it did not authenticate the producer.
        That is a different fact from "the producer matched", and the ingestion path
        that persists evidence must not be able to reach acceptance without it.
        """
        evidence = accept()
        assert evidence.is_complete_review
        assert refusal(reviewer_run_id="run-the-real-authenticated-reviewer") is ReviewEvidenceRefusal.REVIEWER_MISMATCH


class TestExecutionIsBoundServerSide:
    """A persisted artifact must name the exact execution the server resolved."""

    def test_wrong_execution_is_refused(self):
        body = patched(APPROVE, scope={"execution_id": "execution-somewhere-else"})
        assert refusal(body, execution_id="execution-the-real-one") is ReviewEvidenceRefusal.EXECUTION_MISMATCH

    def test_unbound_execution_is_refused_with_its_own_code(self):
        """An absent execution is distinguishable from a wrong one.

        The contract permits a pre-submission producer to omit it, so "not yet
        known" and "names another attempt" are different producer bugs and get
        different codes.
        """
        body = patched(APPROVE, scope={"execution_id": None})
        assert refusal(body, execution_id="execution-the-real-one") is ReviewEvidenceRefusal.EXECUTION_UNBOUND

    def test_matching_execution_is_accepted(self):
        execution_id = APPROVE["scope"]["execution_id"]
        assert accept(execution_id=execution_id).is_complete_review

    def test_pre_submission_producer_may_still_omit_it(self):
        """Backwards compatibility: no server-resolved execution, no check.

        A caller that is only validating a producer's draft does not have an
        execution to compare against, and must not be forced to invent one.
        """
        body = patched(APPROVE, scope={"execution_id": None})
        assert accept(body).is_complete_review


# ---------------------------------------------------------------------------
# A concluded review survives a failed publication
# ---------------------------------------------------------------------------


class TestConcludedReviewSurvivesAFailedPublication:
    """Verdict `approve`, zero blockers, publication HTTP 401 — both facts persist.

    The reported sequence. Previously the contract rejected this document outright
    (approval was gated on publication), so a producer could only drop the artifact
    or relabel the verdict `incomplete`. The first loses the review; the second
    misreports what the reviewer concluded and hides the 401 an operator must act on.

    Two properties are asserted together throughout, because either alone is a
    defect. The functional verdict is preserved, AND nothing here grants approval:
    `is_complete_review` stays False while the verdict is unpublished. Merge
    eligibility remains #5148's decision and is not computed anywhere in this module.
    """

    def test_the_artifact_is_accepted_not_dropped(self):
        evidence = accept(PUBLICATION_FAILED)
        assert evidence.result.verdict.value == "approve", "the reviewer's actual conclusion must survive"

    def test_the_publication_failure_is_preserved(self):
        evidence = accept(PUBLICATION_FAILED)
        assert evidence.result.publication.outcome.value == "failed"
        assert evidence.publication_blockers, "the failure must be carried, not summarised away"
        assert "401" in " ".join(evidence.publication_blockers), "the operator-actionable cause must survive"

    def test_it_is_not_an_approval(self):
        """The preservation must never become autonomous approval."""
        evidence = accept(PUBLICATION_FAILED)
        assert not evidence.is_complete_review
        assert evidence.approval_blockers

    def test_publication_is_identified_as_the_only_thing_outstanding(self):
        evidence = accept(PUBLICATION_FAILED)
        assert evidence.review_concluded_without_blockers
        assert evidence.publication_is_outstanding

    def test_a_real_review_blocker_is_not_reported_as_merely_unpublished(self):
        """The distinction has to cut both ways or it is worthless.

        `REFUSED` has a request-changes verdict *and* an unpublished verdict. If
        `publication_is_outstanding` were True there, an operator would retry a
        publication instead of reading the findings.
        """
        evidence = accept(REFUSED)
        assert not evidence.review_concluded_without_blockers
        assert not evidence.publication_is_outstanding

    def test_a_fully_published_approval_has_nothing_outstanding(self):
        evidence = accept(APPROVE)
        assert evidence.is_complete_review
        assert evidence.publication_blockers == ()
        assert not evidence.publication_is_outstanding

    def test_the_operator_surface_distinguishes_the_two_cases(self):
        """`complete_review` alone cannot answer "why"; both rows must say which."""
        unpublished = evidence_summary(accept(PUBLICATION_FAILED))
        found_blocker = evidence_summary(accept(REFUSED))
        assert unpublished["complete_review"] is False
        assert unpublished["publication_outstanding"] is True
        assert found_blocker["complete_review"] is False
        assert found_blocker["publication_outstanding"] is False

    def test_the_ledger_row_records_which_case_it_was(self):
        intent = evidence_action_intent(accept(PUBLICATION_FAILED))
        assert intent.detail["verdict"] == "approve"
        assert intent.detail["publication"] == "failed"
        assert intent.detail["complete_review"] == "false"
        assert intent.detail["publication_outstanding"] == "true"

    def test_recording_it_is_a_success_not_a_failure(self):
        """The recording succeeded; what failed was the publication.

        Marking the observation FAILED would discard the artifact at the ledger
        boundary — the same loss, one layer down.
        """
        observation = evidence_observation(accept(PUBLICATION_FAILED))
        assert observation.outcome.value == "succeeded"
        assert "401" in (observation.detail or "")

    def test_a_later_successful_publication_is_a_distinct_ledger_action(self):
        """Retrying publication at the unchanged head must not collide with the failure.

        This is the head-alone deduplication defect, in the exact shape that produced
        it: same node, same cycle, same commit, different result. If these keys
        matched, the successful publication would be refused as already settled and
        the 401 would be the permanent record. The real-store sequence is
        `test_review_evidence_postgres.py`; here the property is key distinctness.
        """
        failed = accept(PUBLICATION_FAILED)
        published = {
            "outcome": "published",
            "published_head_sha": failed.reviewed_head_sha,
            "reference": "pullrequestreview-5229910599",
            "detail": None,
        }
        body = patched(PUBLICATION_FAILED, publication=published)
        body["result_id"] = f"{PUBLICATION_FAILED['result_id']}-retry"
        # State passed explicitly: `accept` keys its default state on `result_id`, and
        # the retry deliberately carries a new one. The state is that of the document
        # it is derived from, so the only difference under test is the result identity.
        retried = validate_review_result(body, **matching_state(PUBLICATION_FAILED))

        assert retried.reviewed_head_sha == failed.reviewed_head_sha, "the head must be unchanged for this to be the reported case"
        assert retried.is_complete_review, "the retry published successfully"
        assert evidence_operation_key(retried) != evidence_operation_key(failed)
        assert retried.artifact_ref != failed.artifact_ref

    def test_replaying_the_same_failed_result_still_converges(self):
        """Preserving the failure must not turn a redelivery into a second review."""
        first = accept(PUBLICATION_FAILED)
        again = accept(PUBLICATION_FAILED)
        assert evidence_operation_key(again) == evidence_operation_key(first)
        assert again.artifact_ref == first.artifact_ref


# ---------------------------------------------------------------------------
# Ledger identity: per result, not per head
# ---------------------------------------------------------------------------


def _result_variant(reviewer: str, result_id: str) -> dict:
    body = patched(APPROVE, lineage={"reviewer_run_id": reviewer})
    body["result_id"] = result_id
    return body


class TestEvidenceIdentityIsPerResult:
    """Two different results at one head must not share a ledger action.

    Reproduced on real PostgreSQL by the supervisor: a first valid *incomplete*
    review whose publication failed with HTTP 401 was recorded and settled, and the
    later complete approval at the **unchanged** head — a distinct `result_id` —
    returned CONFLICT / `action_already_settled`. A settled observation is correctly
    immutable, so the defect was the key, not the store.

    The synthetic store used elsewhere in this file does not reproduce the
    immutability refusal (it replaces unconditionally and always returns APPLIED),
    which is why these assertions are about key distinctness and why the real-store
    sequence is exercised in `test_review_evidence_postgres.py`.
    """

    def test_distinct_results_at_the_same_head_get_distinct_refs(self):
        first = accept(_result_variant("run-reviewer-one", "result-first-attempt"), reviewer_run_id="run-reviewer-one")
        second = accept(_result_variant("run-reviewer-two", "result-second-attempt"), reviewer_run_id="run-reviewer-two")
        assert first.reviewed_head_sha == second.reviewed_head_sha, "the premise is an unchanged head"
        assert first.artifact_ref != second.artifact_ref

    def test_distinct_results_at_the_same_head_get_distinct_operation_keys(self):
        first = accept(_result_variant("run-reviewer-one", "result-first-attempt"), reviewer_run_id="run-reviewer-one")
        second = accept(_result_variant("run-reviewer-two", "result-second-attempt"), reviewer_run_id="run-reviewer-two")
        assert evidence_operation_key(first) != evidence_operation_key(second)

    def test_a_differing_result_id_alone_is_enough_to_separate_them(self):
        """The same reviewer resubmitting a genuinely new result is not a replay."""
        first = accept(_result_variant(REVIEWER_RUN, "result-one"), reviewer_run_id=REVIEWER_RUN)
        second = accept(_result_variant(REVIEWER_RUN, "result-two"), reviewer_run_id=REVIEWER_RUN)
        assert evidence_operation_key(first) != evidence_operation_key(second)
        assert first.artifact_ref != second.artifact_ref

    def test_replaying_the_same_result_still_converges(self):
        """Idempotency is preserved: a retry of one result is still one review.

        This is the property the original head-only key was protecting, and it must
        survive the fix — otherwise every transport retry becomes a second apparent
        review.
        """
        once = accept()
        twice = accept()
        assert evidence_operation_key(once) == evidence_operation_key(twice)
        assert once.artifact_ref == twice.artifact_ref

    def test_a_different_head_is_still_different_evidence(self):
        """The original property: review of another commit gets its own identity.

        `publication` is moved with the subject: the contract requires a published
        verdict to name the commit it was recorded against, so patching only
        `reviewed_head_sha` would be a malformed document rather than a review of a
        second commit.
        """
        moved = patched(
            APPROVE,
            subject={"reviewed_head_sha": MOVED_HEAD},
            publication={"published_head_sha": MOVED_HEAD},
        )
        first = accept()
        second = accept(moved, binding=binding(head=MOVED_HEAD))
        assert second.reviewed_head_sha == MOVED_HEAD
        assert evidence_operation_key(first) != evidence_operation_key(second)

    def test_operation_key_fits_the_ledger_column(self):
        """`operation_key` is String(255); a long producer-chosen result_id must not
        push the key over it, where it would truncate two results into one.
        """
        body = _result_variant(REVIEWER_RUN, "result-" + "x" * 4000)
        key = evidence_operation_key(accept(body, reviewer_run_id=REVIEWER_RUN))
        assert len(key) <= 255

    def test_the_reviewed_commit_is_still_recoverable_from_the_reference(self):
        """An operator reading the row must still see which commit was reviewed."""
        evidence = accept()
        assert HEAD in evidence.artifact_ref
        assert NODE in evidence.artifact_ref
