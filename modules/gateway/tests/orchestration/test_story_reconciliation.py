"""Reconciliation reads the bound PR, and the legacy boundary (#5301).

`test_pr_bindings.py` covers the binding contract in isolation. This file covers
the seam in `results.py` — which evidence path a finished story attempt takes, and
what it does when the association is missing.

The boundary is the subtle half of the fix and worth stating plainly. "No binding →
fall back to issue closure" applied to *every* dispatch would be a hole exactly the
size of the original bug: newly unregistered work would still complete on a closing
keyword, and the unregistered-PR case the issue requires to be refused would pass.
So the marker lives on the run's own dispatch record:

- a dispatch written before this contract carries no marker and keeps the old
  issue-closure path, byte for byte;
- a dispatch written under this contract must produce a binding, and **holds** with
  a stated reason if it has none.

That makes the contract a property of the run rather than an inference from a
wall-clock date, which is what lets both behaviours coexist without one leaking
into the other.

The provider is stubbed here. Whether the GraphQL query returns the right fields is
not what these tests are for; which *path* reconciliation takes, and that an absent
answer never becomes a pass, is.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import (
    ActorKind,
    BindingRole,
    DecisionKind,
    NodeKind,
    NodeState,
    OrchestrationDecision,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.pr_bindings import (
    MergeEvidence,
    PullRequestIdentity,
    recover_binding,
    register_binding,
    resolve_registration_target,
)
from src.orchestration.results import _story_evidence, binding_required
from src.shared.models.base import Base

ORG_A = "org-alpha"
REPO = "aws-e/adp"
ISSUE = 5049
REPO_ID = 987_654_321
PR_NUMBER = 5293
PR_NODE = "PR_kwDOABCD12345"
HEAD = "6c7370387d5d57a6ff9ebb5a567f0744e7d99d0e"
NEW_HEAD = "aaaa111122223333444455556666777788889999"
INSTALLATION = 4242
ARRIVED = "2026-09-17T04:00:00Z"
PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"


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
    async def _resolve(_session, *, org_id):
        return INSTALLATION

    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _resolve)


class StubSource:
    """Stands in for `GitHubEvidenceSource`, recording which path was taken.

    Both methods are present so a test can assert not merely on the outcome but on
    *which question reconciliation asked* — the legacy path must not consult a
    binding, and the binding path must never fall back to asking about the issue.
    """

    def __init__(self, *, issue_url: str | None = None, evidence: MergeEvidence | None = None) -> None:
        self.issue_url = issue_url
        self.evidence = evidence
        self.merged_story_calls = 0
        self.bound_pr_calls: list[tuple[str, int]] = []

    async def merged_story(self, *, org_id, installation_id, repo, issue, since):
        self.merged_story_calls += 1
        return self.issue_url

    async def bound_pull_request(self, *, org_id, installation_id, repo, pr_number):
        self.bound_pr_calls.append((repo, pr_number))
        return self.evidence


def _dispatch(*, run_id: str, binding_marker: bool, attempt: int = 1) -> dict:
    record = {
        "run_id": run_id,
        "attempt": attempt,
        "repo": REPO,
        "issue": ISSUE,
        "arrived_at": ARRIVED,
    }
    if binding_marker:
        record["pr_binding_required"] = True
    return record


async def _story(session, *, binding_marker: bool, attempts: int = 1) -> tuple[OrchestrationNode, dict]:
    flow = OrchestrationFlow(org_id=ORG_A, slug="flow-5049", title="Deliver the epic", state="running")
    session.add(flow)
    await session.flush()
    node = OrchestrationNode(
        org_id=ORG_A,
        flow_id=flow.id,
        epic_ref="epic-1",
        wave_ref="wave-1",
        node_ref=f"story-{ISSUE}",
        kind=NodeKind.STORY.value,
        state=NodeState.AWAITING_MERGE.value,
        title="Implement the thing",
        issue_ref=str(ISSUE),
        attempts=attempts,
    )
    session.add(node)
    await session.flush()
    run_id = attempt_run_id(node.id, attempts)
    dispatch = _dispatch(run_id=run_id, binding_marker=binding_marker, attempt=attempts)
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=flow.id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="engine",
            actor_role="service",
            actor_kind=ActorKind.SERVICE.value,
            reason=json.dumps(dispatch),
        )
    )
    await session.flush()
    return node, dispatch


async def _bind(session, node, dispatch, *, head: str = HEAD, role: BindingRole | None = None):
    target = await resolve_registration_target(session, run_id=dispatch["run_id"])
    binding, _ = await register_binding(
        session,
        target=target,
        pr=PullRequestIdentity(
            provider_repository_id=REPO_ID,
            provider_pr_node_id=PR_NODE,
            repo=REPO,
            pr_number=PR_NUMBER,
            head_sha=head,
        ),
        actor_id="scaledjob-worker",
        actor_kind=ActorKind.SERVICE,
        declared_role=role,
    )
    return binding


def _green(**overrides) -> MergeEvidence:
    fields = {
        "merged": True,
        "provider_repository_id": REPO_ID,
        "provider_pr_node_id": PR_NODE,
        "merged_at": "2026-09-17T04:19:36Z",
        "head_sha": HEAD,
        "merge_commit_sha": HEAD,
        "checks_successful": True,
        "approved_by_non_author": True,
        "url": PR_URL,
    }
    fields.update(overrides)
    return MergeEvidence(**fields)


# ---------------------------------------------------------------------------
# The marker decides the path
# ---------------------------------------------------------------------------


def test_marker_absent_means_legacy():
    assert binding_required({"run_id": "orch:x"}) is False
    assert binding_required({"pr_binding_required": True}) is True
    assert binding_required({"pr_binding_required": False}) is False


# ---------------------------------------------------------------------------
# The reproduction, at the reconciliation seam
# ---------------------------------------------------------------------------


async def test_bound_merged_pr_completes_without_consulting_the_issue(session):
    """The acceptance case: the issue is never asked about, so its state is irrelevant.

    `merged_story_calls == 0` is the assertion that matters. The original bug was
    entirely a consequence of asking that question; a story whose PR is bound must
    complete without it.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    source = StubSource(issue_url=None, evidence=_green())

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url == PR_URL
    assert hold == ""
    assert source.merged_story_calls == 0
    assert source.bound_pr_calls == [(REPO, PR_NUMBER)]


# ---------------------------------------------------------------------------
# Binding-required holds — each names its own reason
# ---------------------------------------------------------------------------


async def test_unregistered_binding_required_story_holds(session):
    """The negative the universal fallback would have broken.

    The issue *is* closed here, and by a merged PR — the legacy path would pass this
    story. Because the dispatch required a binding and none exists, it must hold.
    """
    node, dispatch = await _story(session, binding_marker=True)
    source = StubSource(issue_url=PR_URL, evidence=_green())

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url is None
    assert "No pull request is registered" in hold
    assert source.merged_story_calls == 0, "a binding-required story must not fall back to issue closure"


async def test_manual_issue_closure_does_not_complete_a_bound_story(session):
    """Closing the issue by hand is not delivery evidence.

    Same shape as above with a binding present but its PR unmerged: the closed issue
    must not substitute for the PR's own merge.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    source = StubSource(issue_url=PR_URL, evidence=_green(merged=False))

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url is None
    assert "not merged" in hold.lower()
    assert source.merged_story_calls == 0


async def test_worker_success_alone_does_not_complete(session):
    """Reconciliation asks the provider about the PR; the run's own exit proves nothing.

    Modelled as the provider having no answer yet. An absent answer is a hold.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    source = StubSource(evidence=None)

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url is None
    assert hold


async def test_moved_head_holds_for_fresh_evidence(session):
    """New commits after registration invalidate the earlier review and checks."""
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    source = StubSource(evidence=_green(head_sha=NEW_HEAD))

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url is None
    assert "new commits" in hold.lower()
    assert "fresh" in hold.lower()


async def test_no_independent_review_holds_with_named_reason(session):
    """The U11 merge shape: merged and green, no approval from a non-author."""
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    source = StubSource(evidence=_green(approved_by_non_author=False))

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url is None
    assert "reviewer other than the author" in hold


async def test_reviewer_artifact_holds_without_a_provider_call(session):
    """Nothing GitHub could say makes a reviewer artifact into delivery."""
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch, role=BindingRole.REVIEWER_ARTIFACT)
    source = StubSource(evidence=_green())

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url is None
    assert "reviewer artifact" in hold
    assert source.bound_pr_calls == [], "a structurally ineligible binding must not cost a provider call"


async def test_installation_mismatch_holds(session):
    """Reading the PR through a different installation could answer about another repo."""
    node, dispatch = await _story(session, binding_marker=True)
    await _bind(session, node, dispatch)
    source = StubSource(evidence=_green())

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION + 1)

    assert url is None
    assert hold
    assert source.bound_pr_calls == []


# ---------------------------------------------------------------------------
# The legacy boundary: old dispatches keep old behaviour exactly
# ---------------------------------------------------------------------------


async def test_legacy_dispatch_still_completes_from_issue_closure(session):
    """A run dispatched before this contract keeps the issue-closure path."""
    node, dispatch = await _story(session, binding_marker=False)
    source = StubSource(issue_url=PR_URL)

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url == PR_URL
    assert source.merged_story_calls == 1
    assert source.bound_pr_calls == [], "a legacy dispatch must not consult a binding"


async def test_legacy_dispatch_waiting_keeps_the_original_message(session):
    """Unchanged prose for unchanged behaviour, so legacy holds read as they did."""
    node, dispatch = await _story(session, binding_marker=False)
    source = StubSource(issue_url=None)

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url is None
    assert hold == "Agent finished. Waiting for the issue to be completed by a merged pull request with successful checks."


async def test_legacy_dispatch_uses_its_verified_binding(session):
    """Recovery must take precedence over the historical issue-closure fallback."""
    node, dispatch = await _story(session, binding_marker=False)
    marked = dict(dispatch)
    marked["pr_binding_required"] = True
    await _bind(session, node, marked)
    source = StubSource(issue_url=None, evidence=_green())

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url == PR_URL
    assert source.merged_story_calls == 0
    assert source.bound_pr_calls == [(REPO, PR_NUMBER)]


# ---------------------------------------------------------------------------
# Attributed recovery unblocks a stranded story — but proves nothing by itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("binding_marker", [False, True])
async def test_recovered_binding_lets_a_stranded_story_reconcile(session, binding_marker):
    """The path for stories this fix would otherwise leave permanently held.

    Their dispatch required a binding (or has been re-dispatched under one) but no
    run ever registered it. After an attributed recovery, reconciliation reads the
    bound PR exactly as it would for a self-registered one.
    """
    node, dispatch = await _story(session, binding_marker=binding_marker)
    source = StubSource(evidence=_green())

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)
    assert url is None

    await recover_binding(
        session,
        org_id=ORG_A,
        node_id=node.id,
        pr=PullRequestIdentity(
            provider_repository_id=REPO_ID,
            provider_pr_node_id=PR_NODE,
            repo=REPO,
            pr_number=PR_NUMBER,
            head_sha=HEAD,
        ),
        installation_id=INSTALLATION,
        actor_id="operator@example.com",
        reason="delivered before pull-request binding existed",
    )

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)
    assert url == PR_URL
    assert hold == ""


async def test_recovery_cannot_complete_a_story_whose_evidence_is_missing(session):
    """Recording an association is not the same as satisfying the evidence.

    The guard that stops recovery from becoming a way to pass work by asserting it.
    Uses the U11 evidence shape — merged and green, no independent approval — so a
    recovery of exactly that PR still holds.
    """
    node, dispatch = await _story(session, binding_marker=True)
    await recover_binding(
        session,
        org_id=ORG_A,
        node_id=node.id,
        pr=PullRequestIdentity(
            provider_repository_id=REPO_ID,
            provider_pr_node_id=PR_NODE,
            repo=REPO,
            pr_number=PR_NUMBER,
            head_sha=HEAD,
        ),
        installation_id=INSTALLATION,
        actor_id="operator@example.com",
        reason="attesting the implementing pull request",
    )
    source = StubSource(evidence=_green(approved_by_non_author=False))

    url, hold = await _story_evidence(session, node=node, dispatch=dispatch, source=source, installation_id=INSTALLATION)

    assert url is None
    assert "reviewer other than the author" in hold
