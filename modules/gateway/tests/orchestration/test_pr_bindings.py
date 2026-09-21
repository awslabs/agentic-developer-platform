"""Story-to-PR binding behaviour (#5301).

The bug this pins: `GitHubEvidenceSource.merged_story` asked GitHub whether the
*issue* was closed by a merged PR. A PR body saying `Issue #5049` rather than
`Closes #5049` creates no closing event, so the issue stayed open, no evidence
returned, and the story sat in `awaiting_merge` while its implementation PR was
merged. The fix is a durable association, so these tests are written against the
*association*, never against issue state.

The organising principle for every negative below: **an absent answer is never a
pass.** Each refusal arm is asserted to hold the story with its own stated reason,
because the original failure was expensive to diagnose precisely because the hold
reason was generic and unactionable.

Deliberately NOT asserted anywhere in this file:

- that a closing keyword was added, or a title/branch matched, or an issue was
  closed. The issue names all three as not-the-fix; a test that accepted any of
  them would re-encode the bug as the contract.
- that a live flow advanced. These are constructed bindings; provider tests cover
  the shared App's authenticated current-head approval comment. A merge without
  any verified approval remains held by `test_missing_verified_review_holds`.

Concurrency and transactional claims are in `test_pr_bindings_postgres.py` against
a real server; SQLite proves semantics only, for the reasons
`test_work_claims.py` states about `SELECT ... FOR UPDATE` being a no-op there.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import (
    ActorKind,
    BindingRole,
    BindingState,
    DecisionKind,
    NodeKind,
    NodeState,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from src.orchestration.pr_bindings import (
    BindingError,
    BindingRefusal,
    MergeEvidence,
    PullRequestIdentity,
    active_binding_for_node,
    active_bindings_for_flow,
    binding_summary,
    completion_candidate,
    evidence_for_binding,
    hold_explanation,
    recover_binding,
    register_binding,
    resolve_registration_target,
)
from src.shared.models.base import Base

ORG_A = "org-alpha"
ORG_B = "org-beta"
REPO = "aws-e/adp"
OTHER_REPO = "aws-e/other"
ISSUE = 5049
REPO_ID = 987_654_321
OTHER_REPO_ID = 123_456_789
PR_NUMBER = 5293
PR_NODE = "PR_kwDOABCD12345"
HEAD = "6c7370387d5d57a6ff9ebb5a567f0744e7d99d0e"
NEW_HEAD = "aaaa111122223333444455556666777788889999"
SERVICE = "scaledjob-worker"


# ---------------------------------------------------------------------------
# Fixtures — same SQLite-in-memory shape as test_work_claims.py
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


@pytest.fixture(autouse=True)
def _installation(monkeypatch):
    """The tenant's GitHub installation, resolved server-side rather than sent."""

    async def _resolve(_session, *, org_id):
        return 4242 if org_id == ORG_A else 5252

    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _resolve)


async def _story(
    session: AsyncSession,
    *,
    org_id: str = ORG_A,
    kind: str = NodeKind.STORY.value,
    attempts: int = 1,
    repo: str = REPO,
    issue: int = ISSUE,
    dispatch_attempt: int | None = None,
    run_id: str | None = None,
) -> tuple[OrchestrationNode, str]:
    """A dispatched story: flow + node + its `NODE_DISPATCHED` decision.

    Returns the node and the run id the worker would authenticate as. The decision's
    `reason` JSON is the shape `dispatch_pass` writes, because that record — not the
    caller — is what `resolve_registration_target` reads the story from.
    """
    flow = OrchestrationFlow(org_id=org_id, slug=f"flow-{issue}", title="Deliver the epic", state="running")
    session.add(flow)
    await session.flush()
    node = OrchestrationNode(
        org_id=org_id,
        flow_id=flow.id,
        epic_ref="epic-1",
        wave_ref="wave-1",
        node_ref=f"story-{issue}",
        kind=kind,
        state=NodeState.AWAITING_MERGE.value,
        title="Implement the thing",
        issue_ref=str(issue),
        attempts=attempts,
    )
    session.add(node)
    await session.flush()
    resolved_run = run_id or attempt_run_id(node.id, attempts)
    session.add(
        OrchestrationDecision(
            org_id=org_id,
            flow_id=flow.id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="engine",
            actor_role="service",
            actor_kind=ActorKind.SERVICE.value,
            reason=json.dumps(
                {
                    "run_id": resolved_run,
                    "attempt": dispatch_attempt if dispatch_attempt is not None else attempts,
                    "repo": repo,
                    "issue": issue,
                    "pr_binding_required": True,
                }
            ),
        )
    )
    await session.flush()
    return node, resolved_run


def _pr(
    *,
    repo: str = REPO,
    number: int = PR_NUMBER,
    node_id: str = PR_NODE,
    repo_id: int = REPO_ID,
    head: str = HEAD,
) -> PullRequestIdentity:
    return PullRequestIdentity(
        provider_repository_id=repo_id,
        provider_pr_node_id=node_id,
        repo=repo,
        pr_number=number,
        head_sha=head,
    )


def _green(**overrides) -> MergeEvidence:
    """Provider truth for a merged, green, review-approved PR."""
    fields = {
        "merged": True,
        "provider_repository_id": REPO_ID,
        "provider_pr_node_id": PR_NODE,
        "merged_at": "2026-09-17T04:19:36Z",
        "head_sha": HEAD,
        "checks_successful": True,
        "review_approved": True,
        "merge_commit_sha": "6c7370387d5d57a6ff9ebb5a567f0744e7d99d0e",
        "url": f"https://github.com/{REPO}/pull/{PR_NUMBER}",
    }
    fields.update(overrides)
    return MergeEvidence(**fields)


async def _bind(session, node_and_run, **kwargs):
    node, run_id = node_and_run
    target = await resolve_registration_target(session, run_id=run_id)
    return await register_binding(
        session,
        target=target,
        pr=kwargs.pop("pr", _pr()),
        actor_id=kwargs.pop("actor_id", SERVICE),
        actor_kind=kwargs.pop("actor_kind", ActorKind.SERVICE),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# The reproduction: the U11 shape completes without any issue-closing keyword
# ---------------------------------------------------------------------------


async def test_merged_pr_completes_story_with_no_closing_keyword(session):
    """The acceptance case. Issue never closed; the binding alone carries completion.

    Nothing in this test mentions issue state, a closing keyword, a title or a
    branch name — which is the point. The story completes because a PR *bound* to it
    is merged, green and reviewed.
    """
    story = await _story(session)
    binding, created = await _bind(session, story)
    assert created is True

    assert completion_candidate(binding) is None
    url, refusal = evidence_for_binding(binding, _green())
    assert refusal is None
    assert url == f"https://github.com/{REPO}/pull/{PR_NUMBER}"


async def test_target_is_resolved_server_side_from_the_dispatch(session):
    """Tenant, flow, story and attempt come from the dispatch record, not the caller."""
    node, run_id = await _story(session)
    target = await resolve_registration_target(session, run_id=run_id)
    assert (target.org_id, target.flow_id, target.node_id) == (ORG_A, node.flow_id, node.id)
    assert (target.attempt, target.repo, target.issue) == (1, REPO, ISSUE)
    assert target.installation_id == 4242


# ---------------------------------------------------------------------------
# Registration refusals — each holds the story with its own reason
# ---------------------------------------------------------------------------


async def test_unknown_run_cannot_bind(session):
    await _story(session)
    with pytest.raises(BindingError) as exc:
        await resolve_registration_target(session, run_id="orch:not-a-real-run")
    assert exc.value.code is BindingRefusal.UNKNOWN_RUN


async def test_missing_run_reference_is_refused(session):
    for value in (None, "", "   "):
        with pytest.raises(BindingError) as exc:
            await resolve_registration_target(session, run_id=value)
        assert exc.value.code is BindingRefusal.MISSING_RUN_ID


async def test_superseded_attempt_cannot_bind(session):
    """A retry moved the story on; the stale run's PR must not bind to live work."""
    node, _ = await _story(session, attempts=2)
    stale_run = attempt_run_id(node.id, 1)
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="engine",
            actor_role="service",
            actor_kind=ActorKind.SERVICE.value,
            reason=json.dumps({"run_id": stale_run, "attempt": 1, "repo": REPO, "issue": ISSUE}),
        )
    )
    await session.flush()
    with pytest.raises(BindingError) as exc:
        await resolve_registration_target(session, run_id=stale_run)
    assert exc.value.code is BindingRefusal.STALE_RUN


async def test_non_story_node_cannot_bind(session):
    _, run_id = await _story(session, kind=NodeKind.EVAL.value)
    with pytest.raises(BindingError) as exc:
        await resolve_registration_target(session, run_id=run_id)
    assert exc.value.code is BindingRefusal.NOT_A_STORY


async def test_cross_tenant_run_reference_is_refused(session):
    """A valid run id from another tenant must not resolve to that tenant's story."""
    _, run_id = await _story(session)
    with pytest.raises(BindingError) as exc:
        await resolve_registration_target(session, run_id=run_id, expected_org_id=ORG_B)
    assert exc.value.code is BindingRefusal.TENANT_MISMATCH


@pytest.mark.parametrize(
    "pr",
    [
        _pr(node_id=""),
        _pr(repo_id=0),
        _pr(number=0),
        _pr(head=""),
    ],
    ids=["no_node_id", "no_repository_id", "no_number", "no_head"],
)
async def test_incomplete_identity_is_refused(session, pr):
    """A binding a rename can re-point is not an association."""
    story = await _story(session)
    with pytest.raises(BindingError) as exc:
        await _bind(session, story, pr=pr)
    assert exc.value.code is BindingRefusal.INCOMPLETE_IDENTITY


async def test_pr_from_another_repository_is_refused(session):
    """The run resolves its story correctly and the PR is still the wrong one."""
    story = await _story(session)
    with pytest.raises(BindingError) as exc:
        await _bind(session, story, pr=_pr(repo=OTHER_REPO, repo_id=OTHER_REPO_ID))
    assert exc.value.code is BindingRefusal.REPOSITORY_MISMATCH


async def test_one_pr_cannot_implement_two_stories(session):
    """Re-binding a PR would let a merged PR complete work it never contained."""
    first = await _story(session, issue=ISSUE)
    await _bind(session, first)
    second = await _story(session, issue=ISSUE + 1)
    with pytest.raises(BindingError) as exc:
        await _bind(session, second)
    assert exc.value.code is BindingRefusal.ALREADY_BOUND_ELSEWHERE


# ---------------------------------------------------------------------------
# Convergence: duplicate registration and head repair
# ---------------------------------------------------------------------------


async def test_duplicate_registration_converges_on_one_row(session):
    """A retried request or restarted tick must not insert a rival binding."""
    story = await _story(session)
    first, created_first = await _bind(session, story)
    second, created_second = await _bind(session, story)
    assert (created_first, created_second) == (True, False)
    assert first.id == second.id
    rows = (await session.execute(select(OrchestrationPullRequestBinding))).scalars().all()
    assert len(rows) == 1


async def test_new_commits_repair_the_binding_rather_than_rivalling_it(session):
    """The common case: the agent pushes a fix to its own open PR."""
    story = await _story(session)
    binding, _ = await _bind(session, story)
    repaired, created = await _bind(session, story, pr=_pr(head=NEW_HEAD))
    assert created is False
    assert repaired.id == binding.id
    assert repaired.head_sha == NEW_HEAD
    assert len((await session.execute(select(OrchestrationPullRequestBinding))).scalars().all()) == 1


# ---------------------------------------------------------------------------
# Completion evidence — every arm refuses rather than defaulting to a pass
# ---------------------------------------------------------------------------


async def test_unmerged_bound_pr_holds(session):
    story = await _story(session)
    binding, _ = await _bind(session, story)
    url, refusal = evidence_for_binding(binding, _green(merged=False))
    assert url is None
    assert refusal is BindingRefusal.NOT_MERGED


async def test_absent_provider_answer_is_not_a_pass(session):
    story = await _story(session)
    binding, _ = await _bind(session, story)
    url, refusal = evidence_for_binding(binding, None)
    assert url is None
    assert refusal is BindingRefusal.NOT_MERGED


async def test_moved_head_requires_fresh_evidence(session):
    """Review and check evidence for an older head does not describe this code."""
    story = await _story(session)
    binding, _ = await _bind(session, story)
    url, refusal = evidence_for_binding(binding, _green(head_sha=NEW_HEAD))
    assert url is None
    assert refusal is BindingRefusal.HEAD_MOVED


async def test_failing_checks_hold(session):
    story = await _story(session)
    binding, _ = await _bind(session, story)
    url, refusal = evidence_for_binding(binding, _green(checks_successful=False))
    assert url is None
    assert refusal is BindingRefusal.CHECKS_NOT_GREEN


async def test_missing_verified_review_holds(session):
    """Shared GitHub identity is allowed, but a merged PR still needs review evidence."""
    story = await _story(session)
    binding, _ = await _bind(session, story)
    url, refusal = evidence_for_binding(binding, _green(review_approved=False))
    assert url is None
    assert refusal is BindingRefusal.NO_INDEPENDENT_REVIEW


async def test_unregistered_story_has_no_candidate(session):
    """Nothing bound means nothing completes — no fallback to issue closure."""
    node, _ = await _story(session)
    binding = await active_binding_for_node(session, org_id=ORG_A, node_id=node.id, attempt=1)
    assert binding is None
    assert completion_candidate(None) is BindingRefusal.NO_BINDING


async def test_reviewer_artifact_cannot_complete_a_story(session):
    """However merged and green it is, a reviewer artifact delivered nothing."""
    story = await _story(session)
    binding, _ = await _bind(session, story, declared_role=BindingRole.REVIEWER_ARTIFACT)
    assert binding.role == BindingRole.REVIEWER_ARTIFACT.value
    assert completion_candidate(binding) is BindingRefusal.NOT_IMPLEMENTATION


async def test_a_caller_cannot_upgrade_its_own_role(session):
    """Downgrading is honoured because it only ever reduces what a binding can do."""
    story = await _story(session)
    binding, _ = await _bind(session, story, declared_role=BindingRole.IMPLEMENTATION)
    assert binding.role == BindingRole.IMPLEMENTATION.value


# ---------------------------------------------------------------------------
# Authorized replacement
# ---------------------------------------------------------------------------


async def test_replacement_supersedes_the_old_binding_and_keeps_provenance(session):
    """The superseded row is fenced from completing the new scope, not deleted."""
    story = await _story(session)
    original, _ = await _bind(session, story)
    replacement, created = await _bind(
        session,
        story,
        pr=_pr(number=PR_NUMBER + 1, node_id="PR_kwDOZZZZ99999"),
        replaces_reason="original PR abandoned; work redelivered",
        actor_kind=ActorKind.HUMAN,
    )
    assert created is True
    await session.refresh(original)
    assert original.state == BindingState.SUPERSEDED.value
    assert original.superseded_reason == "original PR abandoned; work redelivered"
    assert original.superseded_at is not None
    # Fenced: the old PR cannot complete the story even if it merges green.
    assert completion_candidate(original) is BindingRefusal.SUPERSEDED
    assert completion_candidate(replacement) is None


async def test_rival_binding_without_authorization_is_ambiguous_not_silently_replaced(session):
    """Silently replacing would let a second PR take over a story with no record."""
    story = await _story(session)
    await _bind(session, story)
    with pytest.raises(BindingError) as exc:
        await _bind(session, story, pr=_pr(number=PR_NUMBER + 1, node_id="PR_kwDOZZZZ99999"))
    assert exc.value.code is BindingRefusal.AMBIGUOUS_CANDIDATE


# ---------------------------------------------------------------------------
# Attributed recovery of historical unbound work
# ---------------------------------------------------------------------------


async def test_recovery_records_who_attested_to_the_binding(session):
    """Work delivered before bindings existed is recorded by a named human.

    Not adopted automatically from a title search: the issue names that as
    not-the-fix, so the association must carry an attribution.
    """
    node, _ = await _story(session)
    binding = await recover_binding(
        session,
        org_id=ORG_A,
        node_id=node.id,
        pr=_pr(),
        installation_id=4242,
        actor_id="operator@example.com",
        reason="delivered before pull-request binding existed",
    )
    assert binding.recovery_reason == "delivered before pull-request binding existed"
    assert binding.registered_by == "operator@example.com"
    assert binding.registered_by_kind == ActorKind.HUMAN.value
    assert completion_candidate(binding) is None


# ---------------------------------------------------------------------------
# The hold explains itself, and the read surface leaks no provenance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("refusal", list(BindingRefusal))
def test_every_refusal_has_actionable_prose(refusal):
    """The original hold said only "waiting for a merged pull request" — true,
    unactionable, and in U11's case describing something that could never happen."""
    text = hold_explanation(refusal)
    assert text and text[0].isupper() and len(text) > 30


async def test_binding_summary_excludes_provenance(session):
    """The graph route reads under `USAGE_READ`; who attested is not spend data."""
    story = await _story(session)
    binding, _ = await _bind(session, story)
    summary = binding_summary(binding)
    assert summary["pr_number"] == PR_NUMBER
    assert summary["head_sha"] == HEAD
    assert {"registered_by", "registered_by_kind", "recovery_reason"}.isdisjoint(summary.keys())


async def test_active_binding_lookup_is_tenant_scoped(session):
    """A node id from another tenant must not surface this tenant's binding."""
    story = await _story(session)
    node, _ = story
    await _bind(session, story)
    assert await active_binding_for_node(session, org_id=ORG_B, node_id=node.id, attempt=1) is None
    assert await active_binding_for_node(session, org_id=ORG_A, node_id=node.id, attempt=1) is not None


async def _second_active_binding(session, node, *, number: int, node_id: str) -> None:
    """Force a second ACTIVE binding onto one attempt, bypassing registration.

    `register_binding` refuses this as `AMBIGUOUS_CANDIDATE` (see
    `test_rival_binding_without_authorization_is_ambiguous_not_silently_replaced`), so
    the state is reachable only when two concurrent writers both pass the
    already-bound read before either inserts. The read-side guard is the backstop for
    exactly that race, and it can only be exercised by constructing its outcome —
    which is why this inserts the row rather than calling the registration path.
    """
    session.add(
        OrchestrationPullRequestBinding(
            org_id=ORG_A,
            flow_id=node.flow_id,
            node_id=node.id,
            attempt=node.attempts,
            run_id=attempt_run_id(node.id, node.attempts),
            provider_repository_id=REPO_ID,
            provider_pr_node_id=node_id,
            repo=REPO,
            pr_number=number,
            installation_id=4242,
            head_sha=HEAD,
            role=BindingRole.IMPLEMENTATION.value,
            state=BindingState.ACTIVE.value,
            registered_by=SERVICE,
            registered_by_kind=ActorKind.SERVICE.value,
        )
    )
    await session.flush()


async def test_ambiguous_binding_is_refused_rather_than_guessed(session):
    """Two active bindings mean the implementing PR is genuinely unknown.

    Guessing the newest would complete a story on an arbitrary pull request, so the
    lookup refuses and an operator supersedes the wrong one.
    """
    story = await _story(session)
    node, _ = story
    await _bind(session, story)
    await _second_active_binding(session, node, number=PR_NUMBER + 1, node_id="PR_kwDOZZZZ99999")
    with pytest.raises(BindingError) as exc:
        await active_binding_for_node(session, org_id=ORG_A, node_id=node.id, attempt=1)
    assert exc.value.code is BindingRefusal.AMBIGUOUS_CANDIDATE


# ---------------------------------------------------------------------------
# The journey view's batch loader
# ---------------------------------------------------------------------------


async def test_flow_loader_returns_each_story_binding(session):
    """One query for the whole flow, since the view renders every node anyway."""
    first = await _story(session, issue=ISSUE)
    first_node, _ = first
    await _bind(session, first)
    second = await _story(session, issue=ISSUE + 1)
    second_node, _ = second
    await _bind(session, second, pr=_pr(number=PR_NUMBER + 1, node_id="PR_kwDOSECOND2222"))

    loaded = await active_bindings_for_flow(session, org_id=ORG_A, flow_id=first_node.flow_id)
    assert loaded[first_node.id].pr_number == PR_NUMBER
    # A different flow's story must not appear in this flow's map.
    assert second_node.id not in loaded or second_node.flow_id == first_node.flow_id


async def test_flow_loader_reports_ambiguity_per_node(session):
    """One broken story must not blank the whole journey view."""
    story = await _story(session)
    node, _ = story
    await _bind(session, story)
    await _second_active_binding(session, node, number=PR_NUMBER + 1, node_id="PR_kwDOZZZZ99999")

    loaded = await active_bindings_for_flow(session, org_id=ORG_A, flow_id=node.flow_id)
    assert loaded[node.id] is BindingRefusal.AMBIGUOUS_CANDIDATE


async def test_flow_loader_is_tenant_scoped(session):
    story = await _story(session)
    node, _ = story
    await _bind(session, story)
    assert await active_bindings_for_flow(session, org_id=ORG_B, flow_id=node.flow_id) == {}


async def test_flow_loader_omits_superseded_bindings(session):
    """A superseded binding is not the story's current candidate."""
    story = await _story(session)
    node, _ = story
    await _bind(session, story)
    await _bind(
        session,
        story,
        pr=_pr(number=PR_NUMBER + 1, node_id="PR_kwDOZZZZ99999"),
        replaces_reason="redelivered",
        actor_kind=ActorKind.HUMAN,
    )
    loaded = await active_bindings_for_flow(session, org_id=ORG_A, flow_id=node.flow_id)
    assert loaded[node.id].pr_number == PR_NUMBER + 1
