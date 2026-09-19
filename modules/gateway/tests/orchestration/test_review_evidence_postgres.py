"""Real-PostgreSQL persistence of review evidence through the ledger (#5146).

This file exists because of one reproduced defect, and the reproduction needed a
real database to happen at all. The sequence:

1. A review at head `H` completes, its formal publication returns HTTP 401, and the
   evidence is recorded and **committed**. Correct: the artifact must survive.
2. Publication is retried at the **unchanged** head `H` and succeeds. A distinct
   review result, with its own `result_id` and its own reviewing run.
3. Recording step 2 returned `CONFLICT` / `action_already_settled`.

The successful publication was refused because the failed one had already settled an
action whose `operation_key` was derived from node, cycle and head — so the two
collapsed onto one row, and the 401 became the permanent record of a pull request
that had in fact been approved and published. That is a stuck story with no
self-resolving path, which is the class of failure this issue exists to remove.

Why PostgreSQL rather than the synthetic store used in `test_review_evidence.py`:
`SyntheticExecutionStore.record_observation` replaces observations unconditionally
and always returns `APPLIED`, so it **cannot** produce `action_already_settled` and
would have reported this suite green throughout. The defect lives in the interaction
between the key and the real store's settled-action immutability — the unique index
on `(org_id, execution_id, operation_key)`, `SELECT ... FOR UPDATE`, and the refusal
to rewrite a `SUCCEEDED` row — none of which SQLite reproduces either. A test that
could not fail on the unfixed code is not evidence that the fix works.

Each step commits in its own session, deliberately. The reported sequence spans
separate deliveries: the 401 was durable before the retry was attempted, and a single
uncommitted transaction would let the second write see state the real second process
never saw.

Skips — never silently passes — when no PostgreSQL server is available. `pgserver`
publishes wheels for Python <= 3.12, which CI's Test job uses. A skip here means
**"not tested"** and must be reported as such, not as a pass.

Verified against a real PostgreSQL 16 server, and verified to be capable of failing:
reverting `evidence_operation_key` to its pre-fix head-only form makes
`test_the_later_success_at_the_same_head_is_also_persisted` fail with exactly the
reported outcome — `OutcomeKind.CONFLICT`, `reason='action_already_settled'` — and
four of the seven tests fail when both keys are reverted. On the fix, all seven pass.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.orchestration.execution_state import ExecutionIdentity, OutcomeKind
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
from src.orchestration.review_evidence import (
    ReviewEvidenceError,
    ReviewEvidenceRefusal,
    evidence_operation_key,
    record_review_evidence,
    validate_review_result,
)
from src.orchestration.work_claims import OwnerKind

# Re-exported through tests/migrations/conftest.py, but this file lives in
# tests/orchestration/, so the fixtures are imported explicitly. `pg_server` is
# session-scoped, so a run that also touches the migration tests shares one server.
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

pytestmark = pytest.mark.integration

# The SAME shared artifact the contract suite and the synthetic-store suite read.
# A hand-written document here could drift from the contract while both files stayed
# green, which is the provenance failure the single-fixture rule exists to prevent.
_REPO_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_PATH = _REPO_ROOT / "contracts" / "orchestration-review" / "v1" / "review-result.golden.json"

with GOLDEN_PATH.open() as fh:
    GOLDEN = json.load(fh)


def _doc(key: str) -> dict:
    return {k: v for k, v in GOLDEN[key].items() if not k.startswith("$")}


#: Verdict `approve`, zero blocking findings, publication failed with HTTP 401 —
#: step 1 of the reported sequence, as a document both sides validate.
FAILED_PUBLICATION = _doc("accepted_result_approve_publication_failed")

ORG = FAILED_PUBLICATION["scope"]["org_id"]
CYCLE = FAILED_PUBLICATION["scope"]["cycle"]
PLAN_VERSION = FAILED_PUBLICATION["authority"]["accepted_plan_version"]
CLAIM = FAILED_PUBLICATION["authority"]["claim_id"]
GENERATION = FAILED_PUBLICATION["authority"]["claim_generation"]
REPO_ID = FAILED_PUBLICATION["repository"]["provider_repository_id"]
REPO = FAILED_PUBLICATION["repository"]["repo"]
PR_NUMBER = FAILED_PUBLICATION["subject"]["pr_number"]
PR_NODE = FAILED_PUBLICATION["subject"]["provider_pr_node_id"]
HEAD = FAILED_PUBLICATION["subject"]["reviewed_head_sha"]
AUTHOR_RUN = FAILED_PUBLICATION["lineage"]["author_run_id"]
REVIEWER_RUN = FAILED_PUBLICATION["lineage"]["reviewer_run_id"]
RETRY_REVIEWER_RUN = "7f3a1c58-9b24-4e01-8d76-2a5e0c94bb31"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def pg_engine(pg_url):  # noqa: F811 - pg_url is a fixture, not a shadowed import
    """An async engine on a fresh database with only the tables under test.

    Built from the ORM models rather than the Alembic chain, for the reason
    `test_execution_store_postgres.py` gives: this file tests runtime behaviour, and
    migration/model agreement is `tests/migrations/`' job.
    """
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
    """A committed flow, plan, claim, node, binding and execution for the golden doc.

    Committed rather than flushed: every step below runs in its own session, exactly
    as the separate deliveries in the reported sequence did.
    """
    async with sessions() as session:
        flow = OrchestrationFlow(org_id=ORG, slug="flow-5146", title="Review evidence", state="draft")
        session.add(flow)
        await session.flush()
        session.add(
            OrchestrationAcceptedPlan(
                org_id=ORG,
                flow_id=flow.id,
                version=PLAN_VERSION,
                plan_document={},
                plan_hash="plan-5146",
            )
        )
        node = OrchestrationNode(
            org_id=ORG,
            flow_id=flow.id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="N1",
            kind="story",
            title="Review evidence",
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
        session.add(
            OrchestrationPullRequestBinding(
                org_id=ORG,
                flow_id=flow.id,
                node_id=node.id,
                attempt=1,
                run_id=AUTHOR_RUN,
                provider_repository_id=REPO_ID,
                provider_pr_node_id=PR_NODE,
                repo=REPO,
                pr_number=PR_NUMBER,
                installation_id=4242,
                head_sha=HEAD,
                revision=1,
                role=BindingRole.IMPLEMENTATION.value,
                state=BindingState.ACTIVE.value,
                registered_by=AUTHOR_RUN,
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
        # Read back rather than assumed: the execution id the ledger will key rows on
        # is whatever the store wrote, and evidence has to name that exact row.
        execution_id = (
            await session.execute(
                select(OrchestrationExecution.id).where(
                    OrchestrationExecution.node_id == node_id,
                    OrchestrationExecution.cycle == CYCLE,
                )
            )
        ).scalar_one()
    return {"identity": identity, "flow_id": flow_id, "node_id": node_id, "execution_id": execution_id}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _document(story, *, node_id: str, **overrides) -> dict:
    """The golden document re-pointed at this test's real node and execution.

    ``execution_id`` carries the row ``create_execution`` actually wrote, resolved in
    the ``story`` fixture. A real producer may emit it unset, but evidence that is
    about to be *persisted* must name the exact execution — so a suite about
    persistence has to supply it, or it is exercising a path production refuses.
    """
    body = json.loads(json.dumps(FAILED_PUBLICATION))
    body["scope"] = {
        **body["scope"],
        "flow_id": story["flow_id"],
        "node_id": node_id,
        "execution_id": story["execution_id"],
    }
    for section, value in overrides.items():
        body[section] = {**body[section], **value} if isinstance(value, dict) else value
    return body


def _trusted_refs(document: dict) -> frozenset[str]:
    """Every reference in the document, as a caller that verified them all would pass."""
    refs = {ref["ref"] for ref in document.get("evidence_refs", [])}
    for finding in document.get("findings", []):
        refs.update(ref["ref"] for ref in finding.get("evidence_refs", []))
    return frozenset(refs)


async def _validated(story, sessions, document):
    """Validate a document against the protected state the server actually holds.

    The binding is loaded from the database rather than constructed, so the head this
    evidence is bound to is the head the server recorded.

    All four protected inputs are supplied — authenticated producer, server-resolved
    execution, freshly-read provider head and verified artifact references — because
    this suite records evidence, and ``record_review_evidence`` refuses anything whose
    protected inputs went unchecked. Supplying them is what a production ingestion
    does; omitting one here would test a path that cannot reach the ledger.
    """
    async with sessions() as session:
        binding = (
            await session.execute(select(OrchestrationPullRequestBinding).where(OrchestrationPullRequestBinding.node_id == story["node_id"]))
        ).scalar_one()
        return validate_review_result(
            document,
            identity=story["identity"],
            binding=binding,
            flow_id=story["flow_id"],
            author_run_id=AUTHOR_RUN,
            reviewer_run_id=document["lineage"]["reviewer_run_id"],
            execution_id=document["scope"]["execution_id"],
            # The head as the provider reports it now. Equal to the binding's head in
            # this suite: the reproduced sequence is two publications at an UNCHANGED
            # head, so a moved head would be a different test.
            actual_head_sha=binding.head_sha,
            trusted_artifact_refs=_trusted_refs(document),
        )


async def _record(story, sessions, evidence):
    """Record evidence and COMMIT, so the next step sees durable state."""
    async with sessions() as session:
        outcome = await record_review_evidence(session, identity=story["identity"], evidence=evidence)
        await session.commit()
        return outcome


def _retry_document(story, *, node_id: str) -> dict:
    """Step 2: the same review republished successfully at the UNCHANGED head.

    A distinct `result_id` and a distinct reviewing run, because it is a distinct
    observation — a later run retried the publication and got a different answer from
    the provider. The head is deliberately identical; that is what made the collision
    reachable.
    """
    body = _document(
        story,
        node_id=node_id,
        publication={
            "outcome": "published",
            "published_head_sha": HEAD,
            "reference": "pullrequestreview-5229910599",
            "detail": None,
        },
        lineage={"reviewer_run_id": RETRY_REVIEWER_RUN},
    )
    body["result_id"] = f"{FAILED_PUBLICATION['result_id']}-retry"
    return body


# ---------------------------------------------------------------------------
# The reported sequence
# ---------------------------------------------------------------------------


class TestFailedPublicationThenAuthorizedSuccess:
    """The reproduced defect, end to end, against a real store."""

    async def test_the_failed_publication_is_persisted(self, story, sessions):
        """Step 1 must be durable. Dropping it loses the review and the 401."""
        evidence = await _validated(story, sessions, _document(story, node_id=story["node_id"]))
        outcome = await _record(story, sessions, evidence)

        assert outcome.kind is OutcomeKind.APPLIED
        assert not evidence.is_complete_review, "an unpublished verdict is not an approval"
        assert evidence.publication_is_outstanding, "the 401 is what is outstanding, not a review finding"

        async with sessions() as session:
            action = (await session.execute(select(OrchestrationAction))).scalar_one()
        assert action.status == "succeeded", "recording succeeded; it is the publication that failed"
        assert action.detail["verdict"] == "approve", "the reviewer's real conclusion must be on the row"
        assert action.detail["publication"] == "failed"
        assert action.detail["publication_outstanding"] == "true"
        assert "401" in (action.detail.get("observation") or ""), "the operator-actionable cause must survive"

    async def test_the_later_success_at_the_same_head_is_also_persisted(self, story, sessions):
        """The headline regression. This returned CONFLICT / action_already_settled.

        Same node, same cycle, same commit, different result. On the unfixed key the
        second write collided with the settled first one, the store correctly refused
        to rewrite a settled action, and the 401 became permanent for a pull request
        that had actually been approved and published.
        """
        first = await _validated(story, sessions, _document(story, node_id=story["node_id"]))
        assert (await _record(story, sessions, first)).kind is OutcomeKind.APPLIED

        second = await _validated(story, sessions, _retry_document(story, node_id=story["node_id"]))
        outcome = await _record(story, sessions, second)

        assert outcome.kind is OutcomeKind.APPLIED, (
            f"the successful publication was refused with reason={outcome.reason!r}; "
            "a retry at an unchanged head must not be swallowed by the failed attempt"
        )
        assert outcome.reason != "action_already_settled"

    async def test_both_observations_survive_as_separate_rows(self, story, sessions):
        """Neither record may overwrite the other: they are different observations.

        Two rows, not one, and the failed attempt keeps its own detail. An
        implementation that made the second write win by *updating* the first row
        would satisfy the test above while still erasing the history an operator
        needs to see that publication had to be retried.
        """
        first = await _validated(story, sessions, _document(story, node_id=story["node_id"]))
        await _record(story, sessions, first)
        second = await _validated(story, sessions, _retry_document(story, node_id=story["node_id"]))
        await _record(story, sessions, second)

        async with sessions() as session:
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()

        assert len(actions) == 2, f"expected the failed attempt and the retry to be distinct rows, got {len(actions)}"
        by_key = {action.operation_key: action for action in actions}
        assert set(by_key) == {evidence_operation_key(first), evidence_operation_key(second)}

        failed = by_key[evidence_operation_key(first)]
        published = by_key[evidence_operation_key(second)]
        assert failed.detail["publication"] == "failed"
        assert published.detail["publication"] == "published"
        assert published.detail["complete_review"] == "true"
        assert failed.detail["complete_review"] == "false"
        assert second.is_complete_review, "the retry published successfully at the reviewed head"

    async def test_the_two_actions_reference_distinct_artifacts(self, story, sessions):
        """Distinct evidence must not point at one artifact reference.

        The reference is how the stored document is found. Sharing it would mean the
        successful publication's artifact was unreachable even though its row existed.
        """
        first = await _validated(story, sessions, _document(story, node_id=story["node_id"]))
        await _record(story, sessions, first)
        second = await _validated(story, sessions, _retry_document(story, node_id=story["node_id"]))
        await _record(story, sessions, second)

        async with sessions() as session:
            refs = (await session.execute(select(OrchestrationAction.artifact_ref))).scalars().all()
        assert len(set(refs)) == 2, f"two observations must not share one artifact reference: {refs}"

    async def test_order_does_not_matter(self, story, sessions):
        """Publish-then-fail must behave the same as fail-then-publish.

        A retry is not always the later event: a publication can succeed and a
        subsequent re-review at the same head can fail in transport. If only one
        ordering worked, the fix would be coincidence rather than correct keying.
        """
        published = await _validated(story, sessions, _retry_document(story, node_id=story["node_id"]))
        assert (await _record(story, sessions, published)).kind is OutcomeKind.APPLIED

        failed = await _validated(story, sessions, _document(story, node_id=story["node_id"]))
        outcome = await _record(story, sessions, failed)

        assert outcome.kind is OutcomeKind.APPLIED, f"reason={outcome.reason!r}"
        async with sessions() as session:
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert len(actions) == 2


class TestRedeliveryStillConverges:
    """Distinguishing results must not turn a retry of ONE result into two reviews.

    The counterweight to the class above, and the reason the key is scoped to result
    identity rather than made unique per call. SQS redelivers, a worker dies after
    writing and before acknowledging, a tick restarts — the same result arrives twice
    and must settle once. A key that included a timestamp or a random value would pass
    every test above and quietly convert each redelivery into an additional recorded
    review.
    """

    async def test_recording_the_same_result_twice_is_idempotent(self, story, sessions):
        document = _document(story, node_id=story["node_id"])
        first = await _validated(story, sessions, document)
        second = await _validated(story, sessions, json.loads(json.dumps(document)))

        assert evidence_operation_key(first) == evidence_operation_key(second)
        assert (await _record(story, sessions, first)).kind is OutcomeKind.APPLIED
        outcome = await _record(story, sessions, second)

        assert outcome.kind is OutcomeKind.APPLIED, f"a byte-identical redelivery must converge, not conflict: {outcome.reason!r}"
        assert outcome.reason == "observation_already_recorded"

        async with sessions() as session:
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert len(actions) == 1, "one result, recorded twice, is one review"

    async def test_a_stale_claim_generation_is_refused_not_recorded(self, story, sessions):
        """Authority is re-checked inside the store's own transaction.

        Validation happens before this write, so a generation that advanced in between
        must be caught here or the ledger would accept evidence from a run whose
        authority had already been revoked.
        """
        evidence = await _validated(story, sessions, _document(story, node_id=story["node_id"]))
        async with sessions() as session:
            claim = await session.get(OrchestrationWorkClaim, CLAIM)
            claim.generation = GENERATION + 1
            await session.commit()

        outcome = await _record(story, sessions, evidence)
        assert outcome.kind is not OutcomeKind.APPLIED, "evidence from a superseded generation must not be recorded"


class TestUncheckedEvidenceCannotReachTheLedger:
    """The fail-closed boundary, asserted against the real store.

    ``validate_review_result`` still accepts a partially-checked document so a draft
    can be validated, which means the only thing standing between "nobody
    authenticated the producer" and a durable row that later readers treat as fact is
    the check inside ``record_review_evidence``. Asserted here rather than only
    against the synthetic store because the claim is about what is *in the database*,
    and a row absent from a real table is the only convincing form of that claim.
    """

    async def _unverified(self, story, sessions, **omit):
        """Evidence validated with one protected input deliberately withheld."""
        document = _document(story, node_id=story["node_id"])
        async with sessions() as session:
            binding = (
                await session.execute(
                    select(OrchestrationPullRequestBinding).where(OrchestrationPullRequestBinding.node_id == story["node_id"])
                )
            ).scalar_one()
        state = {
            "reviewer_run_id": document["lineage"]["reviewer_run_id"],
            "execution_id": document["scope"]["execution_id"],
            "actual_head_sha": binding.head_sha,
            "trusted_artifact_refs": _trusted_refs(document),
        }
        state.update(omit)
        return validate_review_result(
            document,
            identity=story["identity"],
            binding=binding,
            flow_id=story["flow_id"],
            author_run_id=AUTHOR_RUN,
            **state,
        )

    @pytest.mark.parametrize(
        ("omitted", "arm"),
        [
            ("reviewer_run_id", ReviewEvidenceRefusal.REVIEWER_UNVERIFIED),
            ("execution_id", ReviewEvidenceRefusal.EXECUTION_UNVERIFIED),
            ("actual_head_sha", ReviewEvidenceRefusal.HEAD_UNVERIFIED),
            ("trusted_artifact_refs", ReviewEvidenceRefusal.ARTIFACTS_UNVERIFIED),
        ],
    )
    async def test_each_unchecked_input_is_refused_and_writes_nothing(self, story, sessions, omitted, arm):
        evidence = await self._unverified(story, sessions, **{omitted: None})
        assert evidence.is_complete_review is False, f"{omitted} went unchecked and the review still read as complete"

        with pytest.raises(ReviewEvidenceError) as caught:
            await _record(story, sessions, evidence)
        assert caught.value.code is arm

        async with sessions() as session:
            actions = (await session.execute(select(OrchestrationAction))).scalars().all()
        assert actions == [], "a refused write must leave no partial row behind"

    async def test_fully_checked_evidence_still_records(self, story, sessions):
        """The gate must refuse the unchecked case only, not every write."""
        evidence = await self._unverified(story, sessions)
        assert evidence.unverified == ()
        assert (await _record(story, sessions, evidence)).kind is OutcomeKind.APPLIED
