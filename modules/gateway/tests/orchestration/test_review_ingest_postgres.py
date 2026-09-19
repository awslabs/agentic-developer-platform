"""The production observer path, on real PostgreSQL (#5146).

`test_review_evidence.py` tests the validator with protected values handed to it,
and `test_review_evidence_postgres.py` tests that validated evidence persists. Both
take as given the thing this file is about: that a *server* can resolve those
protected values from an authenticated reviewer run and nothing else.

That gap was the whole remaining defect. The validator's `author_run_id` parameter
exists so a reviewer cannot defeat the self-review check by naming a different
author — but with no production caller, the only code resolving `author_run_id`
was the test suite, which read it out of the document it was validating. A check
whose input comes from the thing being checked is not a check. Same for
`execution_id`, the provider head, and the artifact references.

So these tests assert the resolution, not the comparison:

* the authoring run is derived from the **node**, via `attempt_run_id`, and a
  reviewer submitting a document that names a different author is refused;
* the reviewer is the **authenticated** invocation, so a document naming another
  reviewer is refused even though it is a well-formed non-author;
* an attempt that moved while the review ran is refused rather than re-pointed at
  the new attempt;
* references are trusted only when the server confirmed them — by the run's own
  artifact prefix, or by a provider check-run read scoped to the reviewed commit;
* a provider read that *fails* refuses, and is distinguishable from one that
  succeeded and found nothing.

Real PostgreSQL rather than the synthetic store for the reason the sibling suite
gives: the ledger write at the end of the path is only meaningfully exercised
against the real unique index and the real settled-observation immutability. It
also matters here that `identity_for_attempt` and `resolve_expected_subject` are
real queries with real `FOR UPDATE` semantics.

Skips — never silently passes — when no PostgreSQL server is available. A skip
here means "not tested".

The provider is the one thing faked: `resolve_pr_identity` and
`resolve_head_check_runs` are patched, because they are HTTP calls to GitHub. What
is NOT faked is which values the module chooses to read from them, which is the
substance of every test below.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.execution_state import ExecutionIdentity, ExecutionPhase, OutcomeKind
from src.orchestration.execution_store import create_execution
from src.orchestration.models import (
    BindingRole,
    BindingState,
    ClaimState,
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from src.orchestration.pr_bindings import PullRequestIdentity
from src.orchestration.pr_identity import PrIdentityError
from src.orchestration.review_evidence import ReviewEvidenceRefusal
from src.orchestration.review_ingest import ingest_review_result, resolve_review_context, verified_artifact_refs
from src.orchestration.work_claims import OwnerKind
from tests.agentauth.test_artifact_service import artifacts as artifacts_fixture

# Re-exported through tests/migrations/conftest.py, but this file lives in
# tests/orchestration/, so the fixtures are imported explicitly. `pg_server` is
# session-scoped, so a run that also touches the migration tests shares one server.
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

artifacts = artifacts_fixture
pytestmark = pytest.mark.integration

# The same shared artifact every other suite reads. A hand-written document here
# could drift from the contract while staying green, which is the #4029 shape.
_REPO_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_PATH = _REPO_ROOT / "contracts" / "orchestration-review" / "v1" / "review-result.golden.json"

with GOLDEN_PATH.open() as fh:
    GOLDEN = json.load(fh)

APPROVE = {k: v for k, v in GOLDEN["accepted_result_approve"].items() if not k.startswith("$")}

ORG = APPROVE["scope"]["org_id"]
CYCLE = APPROVE["scope"]["cycle"]
PLAN_VERSION = APPROVE["authority"]["accepted_plan_version"]
CLAIM = APPROVE["authority"]["claim_id"]
GENERATION = APPROVE["authority"]["claim_generation"]
REPO_ID = APPROVE["repository"]["provider_repository_id"]
REPO = APPROVE["repository"]["repo"]
PR_NUMBER = APPROVE["subject"]["pr_number"]
PR_NODE = APPROVE["subject"]["provider_pr_node_id"]
HEAD = APPROVE["subject"]["reviewed_head_sha"]
MOVED_HEAD = "f0d2eb968cb5f9d1322da48d92042cd7f45c166a"
INSTALLATION = 4242

#: The reviewer's authenticated invocation. A delegated-dispatch uuid5, deliberately
#: NOT an `attempt_run_id`: a reviewer is dispatched through `graph_dispatch`, which
#: does not mint node-derived ids and does not increment the node's attempt. Tests
#: that used an `attempt_run_id` here would be testing the developer path.
REVIEWER_RUN = "4c1d90a8-55b7-4e2f-9a30-1f6c8ad70b52"

#: This run's server-derived artifact key prefix. A digest of tenant and invocation
#: plus the attempt; a worker cannot choose it.
OWN_PREFIX = "runs/9f2c/5ab1/attempt-1/"


@pytest.fixture
async def pg_engine(pg_url):  # noqa: F811 - pg_url is a fixture, not a shadowed import
    engine = create_async_engine(to_async_url(pg_url), echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(OrchestrationFlow.__table__.create)
        await conn.run_sync(OrchestrationAcceptedPlan.__table__.create)
        await conn.run_sync(OrchestrationNode.__table__.create)
        await conn.run_sync(OrchestrationWorkClaim.__table__.create)
        await conn.run_sync(OrchestrationExecution.__table__.create)
        await conn.run_sync(OrchestrationAction.__table__.create)
        await conn.run_sync(OrchestrationPullRequestBinding.__table__.create)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessions(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
async def story(sessions):
    """A dispatched story at attempt 1, with a bound implementation PR.

    `node.attempts = 1` because the engine increments it when it dispatches the
    developer, and the reviewer is dispatched against that same running attempt
    without incrementing it. That is what makes `attempt_run_id(node, 1)` the
    authoring run, and it is the fact the whole author resolution rests on.
    """
    async with sessions() as session:
        flow = OrchestrationFlow(org_id=ORG, slug="flow-5146-ingest", title="Review ingest", state="draft")
        session.add(flow)
        await session.flush()
        session.add(OrchestrationAcceptedPlan(org_id=ORG, flow_id=flow.id, version=PLAN_VERSION, plan_document={}, plan_hash="plan-5146-ingest"))
        node = OrchestrationNode(
            org_id=ORG,
            flow_id=flow.id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="N1",
            kind="story",
            title="Review ingest",
            state="running",
            attempts=1,
        )
        session.add(node)
        await session.flush()
        session.add(
            OrchestrationWorkClaim(
                id=CLAIM,
                org_id=ORG,
                provider_repository_id=REPO_ID,
                issue_number=5146,
                owner_kind=OwnerKind.ENGINE_FLOW.value,
                owner_ref=flow.id,
                state=ClaimState.HELD.value,
                generation=GENERATION,
            )
        )
        author_run = attempt_run_id(node.id, 1)
        session.add(
            OrchestrationPullRequestBinding(
                org_id=ORG,
                flow_id=flow.id,
                node_id=node.id,
                attempt=1,
                run_id=author_run,
                provider_repository_id=REPO_ID,
                provider_pr_node_id=PR_NODE,
                repo=REPO,
                pr_number=PR_NUMBER,
                installation_id=INSTALLATION,
                head_sha=HEAD,
                revision=1,
                role=BindingRole.IMPLEMENTATION.value,
                state=BindingState.ACTIVE.value,
                registered_by=author_run,
                registered_by_kind="service",
            )
        )
        await session.commit()
        flow_id, node_id = flow.id, node.id

    identity = ExecutionIdentity(
        org_id=ORG,
        node_id=node_id,
        cycle=CYCLE,
        accepted_plan_version=PLAN_VERSION,
        claim_id=CLAIM,
        claim_generation=GENERATION,
    )
    async with sessions() as session:
        await create_execution(session, identity=identity, flow_id=flow_id)
        await session.commit()
    async with sessions() as session:
        execution_id = (await session.execute(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == node_id))).scalar_one()
    return {
        "identity": identity,
        "flow_id": flow_id,
        "node_id": node_id,
        "execution_id": execution_id,
        "author_run": author_run,
    }


def document(story, **overrides) -> dict:
    """The golden approving result, re-pointed at this test's real story.

    The author and reviewer lineage are set to what a *correct* reviewer would emit:
    the author the engine dispatched, and its own authenticated invocation. Tests
    that need a lying document override them explicitly, which keeps "this is what
    an honest submission looks like" in one place.
    """
    body = json.loads(json.dumps(APPROVE))
    body["scope"] = {**body["scope"], "flow_id": story["flow_id"], "node_id": story["node_id"], "execution_id": story["execution_id"]}
    body["lineage"] = {**body["lineage"], "author_run_id": story["author_run"], "reviewer_run_id": REVIEWER_RUN}
    for section, value in overrides.items():
        body[section] = {**body[section], **value} if isinstance(value, dict) else value
    return body


def head_bound_check_refs(body: dict) -> frozenset[str]:
    """The check-run references the document cites for its reviewed head.

    Derived from the document so a provider fake can be told to confirm exactly what
    this document relies on — the "provider agrees" case — rather than a hardcoded
    list that would silently stop matching if the golden fixture changed.
    """
    refs = {ref["ref"] for ref in body.get("evidence_refs", []) if ref.get("head_bound") and ref.get("kind") in {"test-run", "check-run"}}
    for finding in body.get("findings", []):
        refs.update(ref["ref"] for ref in finding.get("evidence_refs", []) if ref.get("head_bound") and ref.get("kind") in {"test-run", "check-run"})
    return frozenset(refs)


def provider(*, head: str = HEAD, checks: frozenset[str] | None = None, identity_error: bool = False, checks_error: bool = False):
    """Patch the two provider reads, returning the (identity, checks) mocks.

    Only the HTTP boundary is faked. Which of these values the module reads, and
    what it does when one fails, is what the tests assert.
    """
    identity_mock = AsyncMock(
        side_effect=PrIdentityError("unavailable") if identity_error else None,
        return_value=PullRequestIdentity(
            provider_repository_id=REPO_ID,
            provider_pr_node_id=PR_NODE,
            repo=REPO,
            pr_number=PR_NUMBER,
            head_sha=head,
        ),
    )
    checks_mock = AsyncMock(
        side_effect=PrIdentityError("unavailable") if checks_error else None,
        return_value=frozenset() if checks is None else checks,
    )
    return patch.multiple(
        "src.orchestration.pr_identity",
        resolve_pr_identity=identity_mock,
        resolve_head_check_runs=checks_mock,
    )


async def resolve(story, sessions, **kwargs):
    async with sessions() as session:
        return await resolve_review_context(
            session,
            org_id=ORG,
            node_id=story["node_id"],
            attempt=kwargs.pop("attempt", 1),
            reviewer_run_id=kwargs.pop("reviewer_run_id", REVIEWER_RUN),
            installation_id=INSTALLATION,
        )


async def ingest(story, sessions, body, *, commit: bool = True, **kwargs):
    async with sessions() as session:
        outcome = await ingest_review_result(
            session,
            document=body,
            org_id=ORG,
            node_id=kwargs.pop("node_id", story["node_id"]),
            attempt=kwargs.pop("attempt", 1),
            reviewer_run_id=kwargs.pop("reviewer_run_id", REVIEWER_RUN),
            installation_id=INSTALLATION,
            own_artifact_prefix=kwargs.pop("own_artifact_prefix", OWN_PREFIX),
        )
        if commit:
            await session.commit()
        return outcome


# ---------------------------------------------------------------------------
# The author is resolved from the node, never from the document
# ---------------------------------------------------------------------------


class TestTheAuthoringRunComesFromProtectedState:
    """The check that made `author_run_id` worth having a parameter for.

    Before this module the only resolver of `author_run_id` was a test helper reading
    it out of the document being validated. This asserts the production path derives
    it from the node the reviewer was dispatched against.
    """

    async def test_the_author_is_derived_from_the_node_and_attempt(self, story, sessions):
        with provider():
            context = await resolve(story, sessions)
        assert context.author_run_id == attempt_run_id(story["node_id"], 1)
        # Not the reviewer, and not anything the document could have said.
        assert context.author_run_id != REVIEWER_RUN

    async def test_a_document_naming_another_author_is_refused(self, story, sessions):
        """The arm that stops a reviewer pointing the self-review check elsewhere."""
        body = document(story, lineage={"author_run_id": "orch:someone-else", "reviewer_run_id": REVIEWER_RUN})
        with provider(checks=head_bound_check_refs(body)):
            outcome = await ingest(story, sessions, body)
        assert outcome.refusal is ReviewEvidenceRefusal.SELF_REVIEW
        assert not outcome.recorded

    async def test_a_reviewer_that_is_the_author_is_refused_before_parsing(self, story, sessions):
        """Refused on authenticated identity alone, without trusting the document.

        The validator also refuses this from the document's lineage. Both matter: the
        document can lie in either direction, and this arm does not depend on it.
        """
        with provider():
            outcome = await ingest(story, sessions, document(story), reviewer_run_id=story["author_run"])
        assert outcome.refusal is ReviewEvidenceRefusal.SELF_REVIEW

    async def test_the_reviewer_is_the_authenticated_run_not_the_named_one(self, story, sessions):
        """A substituted reviewer id is refused even though it is not the author.

        This is the reproduced finding that `reviewer_run_id` was added for: a
        document naming some other non-author run passes the self-review check while
        crediting the review to a run that never performed it.
        """
        body = document(story, lineage={"author_run_id": story["author_run"], "reviewer_run_id": "some-other-run"})
        with provider(checks=head_bound_check_refs(body)):
            outcome = await ingest(story, sessions, body)
        assert outcome.refusal is ReviewEvidenceRefusal.REVIEWER_MISMATCH


# ---------------------------------------------------------------------------
# The attempt and execution are resolved, not assumed
# ---------------------------------------------------------------------------


class TestTheReviewedAttemptMustStillBeCurrent:
    async def test_a_superseded_attempt_is_refused_not_repointed(self, story, sessions):
        """The node moved to attempt 2 while the review ran.

        Refused rather than resolved at attempt 2: the review read attempt 1's code.
        Silently re-pointing it would attach a review to work it never examined,
        which is the wrong-revision failure this whole issue is about.
        """
        async with sessions() as session:
            node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.id == story["node_id"]))
            node.attempts = 2
            await session.commit()
        with provider():
            outcome = await ingest(story, sessions, document(story))
        assert outcome.refusal is ReviewEvidenceRefusal.STALE_CLAIM

    async def test_an_absent_execution_is_refused_and_never_created(self, story, sessions):
        """`NO_EXECUTION`, and nothing was created to make the review fit.

        The row-count assertion comes first deliberately. A mutation that "helpfully"
        created the missing execution was caught by the refusal-arm assertion instead,
        which meant the claim this test is named for never ran — an ordering that would
        stop holding as soon as the substitute arm changed. Asserting absence first
        makes the named claim the one that fails.
        """
        async with sessions() as session:
            execution = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == story["node_id"]))
            await session.delete(execution)
            await session.commit()
        with provider():
            outcome = await ingest(story, sessions, document(story))

        async with sessions() as session:
            remaining = (await session.execute(select(OrchestrationExecution))).scalars().all()
        assert remaining == [], "the ingest path created an execution to make the review fit"
        assert not outcome.recorded
        assert outcome.refusal is ReviewEvidenceRefusal.NO_EXECUTION

    async def test_the_resolved_execution_is_the_row_the_store_wrote(self, story, sessions):
        with provider():
            context = await resolve(story, sessions)
        assert context.execution_id == story["execution_id"]

    async def test_an_unknown_node_is_refused(self, story, sessions):
        with provider():
            outcome = await ingest(story, sessions, document(story), node_id="node-that-does-not-exist")
        assert outcome.refusal is ReviewEvidenceRefusal.NO_EXECUTION


# ---------------------------------------------------------------------------
# The head comes from the provider
# ---------------------------------------------------------------------------


class TestTheHeadIsReadFromTheProvider:
    async def test_the_provider_head_is_used_not_the_cached_binding_column(self, story, sessions):
        """A push moved the head after the binding was registered.

        The binding's cached `head_sha` still says `HEAD` and the document agrees with
        it — both are out of date. Only a provider read can show that, which is why
        the context reads it and why a fallback to the column would be a silent pass.
        """
        with provider(head=MOVED_HEAD):
            context = await resolve(story, sessions)
        assert context.actual_head_sha == MOVED_HEAD
        assert context.binding.head_sha == HEAD

        with provider(head=MOVED_HEAD):
            outcome = await ingest(story, sessions, document(story))
        assert outcome.refusal is ReviewEvidenceRefusal.STALE_HEAD

    async def test_an_unreadable_head_refuses_rather_than_falling_back(self, story, sessions):
        with provider(identity_error=True):
            outcome = await ingest(story, sessions, document(story))
        assert outcome.refusal is ReviewEvidenceRefusal.HEAD_UNVERIFIED
        assert not outcome.recorded


# ---------------------------------------------------------------------------
# References are verified, not believed
# ---------------------------------------------------------------------------


class TestReferencesAreConfirmedBeforeTheyCount:
    async def test_a_cited_check_run_the_provider_confirms_is_trusted(self, story, sessions):
        body = document(story)
        cited = head_bound_check_refs(body)
        assert cited, "the golden document cites no head-bound check run; this test would be vacuous"
        with provider(checks=cited):
            outcome = await ingest(story, sessions, body)
        assert outcome.refusal is None
        assert outcome.recorded

    async def test_a_cited_check_run_the_provider_does_not_confirm_is_refused(self, story, sessions):
        """A real-looking check-run id the provider has no record of at this commit."""
        with provider(checks=frozenset({"check-run:999999999"})):
            outcome = await ingest(story, sessions, document(story))
        assert outcome.refusal is ReviewEvidenceRefusal.UNTRUSTED_ARTIFACT
        assert not outcome.recorded

    async def test_a_provider_read_that_failed_is_not_an_empty_set(self, story, sessions):
        """The distinction this path exists to preserve.

        "Asked, found nothing" and "could not ask" both leave the citation
        unverified, but only one is the reviewer's problem. An empty set would report
        `untrusted_artifact` and send an operator hunting a reviewer defect during a
        provider outage.
        """
        with provider(checks_error=True):
            outcome = await ingest(story, sessions, document(story))
        assert outcome.refusal is ReviewEvidenceRefusal.ARTIFACTS_UNVERIFIED


def _ref(kind: str, ref: str, *, head_bound: bool = True) -> SimpleNamespace:
    """The attributes `verified_artifact_refs` reads, and nothing else.

    A stand-in rather than a contract model, deliberately: the function's contract is
    those three attributes, and building a full validated document to assert a set
    intersection would couple these cases to unrelated required fields.
    """
    return SimpleNamespace(kind=kind, ref=ref, head_bound=head_bound)


class TestWhichReferencesCount:
    """`verified_artifact_refs` on its own — no database, so no skip.

    Split out of the class above because these are pure-function cases. Left on the
    PostgreSQL fixtures they skipped whenever no server was available, reporting "not
    tested" for logic that needs no server — and a skip is not a pass.
    """

    async def test_an_own_artifact_reference_is_trusted_only_under_this_runs_prefix(self):
        """Prefix, not substring, and this run's prefix rather than any run's."""
        mine = _ref("artifact", f"{OWN_PREFIX}review/abc.json")
        theirs = _ref("artifact", "runs/other/other/attempt-1/review/abc.json")
        # A key that merely *contains* this run's prefix. Trusting it would defeat the
        # point of a server-derived namespace, since a worker chooses the rest of the key.
        sneaky = _ref("artifact", f"prefix-confusion/{OWN_PREFIX}review/abc.json")
        result = SimpleNamespace(evidence_refs=[mine, theirs, sneaky], findings=[])

        assert await verified_artifact_refs(result, own_prefix=OWN_PREFIX, provider_check_refs=frozenset()) == frozenset()
        resolver = AsyncMock(return_value=True)
        trusted = await verified_artifact_refs(result, own_prefix=OWN_PREFIX, provider_check_refs=frozenset(), resolve_artifact_ref=resolver)
        assert trusted == frozenset({mine.ref})
        resolver.assert_awaited_once_with(mine.ref)

    async def test_an_unverifiable_kind_is_never_trusted(self):
        """A kind the server cannot resolve must not be waved through.

        Passed a `provider_check_refs` set that *contains* the reference, so a kind
        check that fell through to "is it in either set" would trust it.
        """
        result = SimpleNamespace(evidence_refs=[_ref("reviewer-assertion", "trust-me")], findings=[])
        assert await verified_artifact_refs(result, own_prefix=OWN_PREFIX, provider_check_refs=frozenset({"trust-me"})) == frozenset()

    async def test_an_artifact_is_not_verified_by_the_check_run_set(self):
        """The two routes must not be interchangeable.

        An artifact key that happens to appear in the provider's check-run set is still
        not an own-run upload, and a check-run id under the artifact prefix is still not
        a provider-confirmed run. Verifying either by the other route would make the
        distinction decorative.
        """
        artifact = _ref("artifact", "runs/other/other/attempt-1/x.json")
        check = _ref("check-run", f"{OWN_PREFIX}check-run:1")
        result = SimpleNamespace(evidence_refs=[artifact, check], findings=[])
        confirmed = frozenset({artifact.ref})
        assert await verified_artifact_refs(result, own_prefix=OWN_PREFIX, provider_check_refs=confirmed) == frozenset()

    async def test_finding_level_references_are_verified_too(self):
        """Where a "resolved" citation actually lives.

        A finding citing its own head-bound evidence is the common shape, and checking
        only the top-level list would leave the per-finding ones unverified while the
        result still validated.
        """
        good = _ref("check-run", "check-run:1")
        bad = _ref("check-run", "check-run:2")
        result = SimpleNamespace(
            evidence_refs=[],
            findings=[SimpleNamespace(evidence_refs=[good]), SimpleNamespace(evidence_refs=[bad])],
        )
        trusted = await verified_artifact_refs(result, own_prefix=OWN_PREFIX, provider_check_refs=frozenset({good.ref}))
        assert trusted == frozenset({good.ref})

    async def test_only_head_bound_references_are_reported(self):
        """The returned set is about head-bound references and only those.

        A link to a static design document does not become stale and needs no proving,
        so it is not this function's business. Both references below *would* verify —
        one under this run's prefix, one confirmed by the provider — so the filter is
        what excludes them, not a failure to match. An earlier version of this test
        used a reference that could not verify anyway, and so passed whether or not the
        filter existed.

        The stakes are low but real: the trusted set is compared only against the
        document's head-bound references, so a wider set is inert *today* — and would
        stop being inert the moment a caller used it for anything else.
        """
        static = _ref("artifact", f"{OWN_PREFIX}design-notes.md", head_bound=False)
        check = _ref("check-run", "check-run:9", head_bound=False)
        result = SimpleNamespace(evidence_refs=[static, check], findings=[])
        trusted = await verified_artifact_refs(result, own_prefix=OWN_PREFIX, provider_check_refs=frozenset({check.ref}))
        assert trusted == frozenset()

    async def test_an_empty_own_prefix_trusts_nothing(self):
        """A caller that could not derive the run's namespace verifies no artifact.

        Without this, an empty prefix would make `startswith("")` true for every
        reference and turn a missing namespace into blanket trust — the failure mode
        that is most likely to arise from a refactor and least likely to be noticed.
        """
        result = SimpleNamespace(evidence_refs=[_ref("artifact", f"{OWN_PREFIX}review/abc.json")], findings=[])
        assert await verified_artifact_refs(result, own_prefix="", provider_check_refs=frozenset()) == frozenset()

    async def test_a_non_string_reference_is_skipped_rather_than_raising(self):
        """A malformed reference must not take down the whole submission path."""
        result = SimpleNamespace(evidence_refs=[_ref("artifact", None), _ref("check-run", "check-run:9")], findings=[])
        assert await verified_artifact_refs(result, own_prefix=OWN_PREFIX, provider_check_refs=frozenset({"check-run:9"})) == frozenset(
            {"check-run:9"}
        )


# ---------------------------------------------------------------------------
# The durable record
# ---------------------------------------------------------------------------


class TestEveryProtectedInputIsActuallySupplied:
    """That the observer leaves no protected check unperformed.

    `require_verified_state` refuses evidence with an unchecked input, and the sibling
    suite proves it refuses. What neither proves is that *this* path supplies all four
    — an ingest that dropped one would be refused rather than recorded, so the gate
    converts a silent hole into a loud outage, but only a test that reads `unverified`
    on the success path can say the path is complete rather than merely safe.

    Verified to be capable of failing: dropping any one of the four arguments from
    `ingest_review_result`'s `validate_review_result` call makes this class fail.
    """

    async def test_a_recorded_submission_has_no_unperformed_checks(self, story, sessions):
        body = document(story)
        with provider(checks=head_bound_check_refs(body)):
            outcome = await ingest(story, sessions, body)
        assert outcome.refusal is None, outcome.detail
        assert outcome.evidence.unverified == (), f"the observer skipped a protected check: {outcome.evidence.unverified}"
        assert outcome.evidence.is_complete_review


class TestTheRecordedEvidence:
    async def test_a_complete_submission_lands_in_the_ledger(self, story, sessions):
        body = document(story)
        with provider(checks=head_bound_check_refs(body)):
            outcome = await ingest(story, sessions, body)
        assert outcome.recorded
        assert outcome.ledger.kind is OutcomeKind.APPLIED

        async with sessions() as session:
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert len(actions) == 1
        assert actions[0].kind == "review_evidence"
        assert actions[0].artifact_ref == outcome.evidence.artifact_ref
        assert actions[0].execution_id == story["execution_id"]

    async def test_recording_evidence_decides_nothing(self, story, sessions):
        """The artifact records that a review happened, not that anything may merge.

        Asserted on this path as well as in the validator suite because this is where
        durable state is written, and #5146's scope is the record. A future edit that
        made ingestion advance the phase or move the node would be a merge decision
        taken by an observer, which this issue explicitly does not implement.
        """
        body = document(story)
        with provider(checks=head_bound_check_refs(body)):
            outcome = await ingest(story, sessions, body)
        assert outcome.recorded

        async with sessions() as session:
            execution = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == story["node_id"]))
            node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.id == story["node_id"]))
        # Exactly as `create_execution` left it, and exactly as the fixture left the node.
        assert execution.phase == ExecutionPhase.ADMITTED.value
        assert node.state == "running"
        assert node.attempts == 1

    async def test_the_same_submission_twice_converges(self, story, sessions):
        """A redelivered upload is one review, not two rows."""
        body = document(story)
        with provider(checks=head_bound_check_refs(body)):
            first = await ingest(story, sessions, body)
            second = await ingest(story, sessions, body)
        assert first.recorded
        assert second.evidence is not None
        async with sessions() as session:
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert len(actions) == 1, "a redelivery created a second ledger action"

    async def test_a_refusal_writes_nothing(self, story, sessions):
        with provider(head=MOVED_HEAD):
            outcome = await ingest(story, sessions, document(story))
        assert outcome.refusal is ReviewEvidenceRefusal.STALE_HEAD
        async with sessions() as session:
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert actions == []

    async def test_the_caller_owns_the_commit(self, story, sessions):
        """Nothing persists if the caller rolls back.

        The route re-verifies the caller's authority after the write and before the
        commit, so a credential revoked mid-request must leave no row. That only
        holds if this function does not commit on its own.
        """
        body = document(story)
        with provider(checks=head_bound_check_refs(body)):
            outcome = await ingest(story, sessions, body, commit=False)
        assert outcome.recorded
        async with sessions() as session:
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert actions == [], "the ingest path committed on its own"


@pytest.fixture
async def upload_pipeline(artifacts, story, sessions, monkeypatch):
    """Real artifact route, S3 storage, observer and SQL; mock only external identity."""
    from dataclasses import replace
    from unittest.mock import Mock

    from tests.agentauth.test_run_services import GRANT, RECORD

    client, runtime, storage = artifacts
    record = replace(RECORD, tenant_id=ORG, invocation_id=REVIEWER_RUN, flow_id=story["flow_id"])
    grant = replace(GRANT, tenant_id=ORG, principal=record.principal, flow_id=story["flow_id"])
    runtime.authenticate.return_value = (SimpleNamespace(uid="pod-one"), "caller", record, grant)
    runtime.store = SimpleNamespace(
        _read=Mock(
            return_value={
                "orchestration_node_id": {"S": story["node_id"]},
                "orchestration_node_attempt": {"N": "1"},
                "installation_id": {"N": str(INSTALLATION)},
                "persona": {"S": "reviewer"},
            }
        )
    )
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: sessions)
    return SimpleNamespace(client=client, storage=storage, record=record, runtime=runtime)


async def test_uploaded_bytes_remain_retrievable_after_sql_reload_and_changed_bytes_conflict(upload_pipeline, story, sessions):
    import hashlib
    from urllib.parse import urlsplit

    from src.orchestration.execution_read import _safe_ref
    from tests.agentauth.test_run_services import HEADERS

    pipeline = upload_pipeline
    body = document(story)
    raw = json.dumps(body).encode()
    with provider(checks=head_bound_check_refs(body)):
        first = await pipeline.client.post("/internal/v1/agent/self/artifacts/review-result", content=raw, headers=HEADERS)
        replay = await pipeline.client.post("/internal/v1/agent/self/artifacts/review-result", content=raw, headers=HEADERS)
        body["findings"][0]["summary"] = "Different immutable review content under the same result ID"
        changed = await pipeline.client.post("/internal/v1/agent/self/artifacts/review-result", json=body, headers=HEADERS)
    assert first.status_code == replay.status_code == changed.status_code == 200
    assert first.json() == replay.json()
    assert first.json()["recorded"] is True
    assert changed.json()["recorded"] is False
    assert changed.json()["refusal"] == "not_recorded"
    receipt = first.json()
    async with sessions() as session:
        action = (await session.execute(select(OrchestrationAction).where(OrchestrationAction.org_id == ORG))).scalar_one()
        assert action.artifact_ref == action.receipt_ref == receipt["evidence_ref"]
        assert _safe_ref(action.artifact_ref) == action.artifact_ref
    stored = urlsplit(action.artifact_ref)
    retrieved = pipeline.storage.get_object(Bucket=stored.netloc, Key=stored.path.lstrip("/"))["Body"].read()
    assert retrieved == raw
    assert stored.fragment == "sha256=" + hashlib.sha256(retrieved).hexdigest()
    assert json.loads(retrieved)["findings"][0]["summary"] != body["findings"][0]["summary"]


@pytest.mark.parametrize("storage_state", ["present", "absent", "corrupt", "wrong-kind"])
async def test_artifact_citations_require_actual_stored_content(upload_pipeline, story, storage_state):
    from src.agentauth.artifact_keys import artifact_prefix
    from tests.agentauth.test_run_services import HEADERS

    pipeline = upload_pipeline
    uploaded = await pipeline.client.post("/internal/v1/agent/self/artifacts/transcript", content=b"actual test output", headers=HEADERS)
    key = uploaded.json()["key"]
    if storage_state == "absent":
        pipeline.storage.delete_object(Bucket="run-logs", Key=key)
    elif storage_state == "corrupt":
        pipeline.storage.put_object(Bucket="run-logs", Key=key, Body=b"wrong bytes", ContentType="text/markdown")
    elif storage_state == "wrong-kind":
        key = artifact_prefix(pipeline.record) + "unsupported/" + "a" * 64 + ".json"
    body = document(story)
    body["evidence_refs"].append({"kind": "artifact", "ref": key, "head_bound": True})
    with provider(checks=head_bound_check_refs(body)):
        response = await pipeline.client.post("/internal/v1/agent/self/artifacts/review-result", json=body, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["recorded"] is (storage_state == "present"), response.json()
    if storage_state != "present":
        assert response.json()["refusal"] == "untrusted_artifact"
