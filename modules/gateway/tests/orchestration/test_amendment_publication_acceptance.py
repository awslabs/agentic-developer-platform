"""Publication cannot authorize a draft invalidated before transactional acceptance."""

import asyncio
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from src.orchestration import amendment_issue_publisher as publisher_module
from src.orchestration import amendment_review_routes, repository_binding
from src.orchestration.amend import amend_plan
from src.orchestration.amendment_impact import assess_amendment
from src.orchestration.compile import ProposalRejectedError
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAmendmentIssueBinding,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPendingAmendment,
)
from src.orchestration.pending_amendments import accept_amendment, publish_for_acceptance, register_amendment_draft, revise_amendment
from src.orchestration.proposal import LoopProposal, ProposedEdge, ProposedNode, RepositoryConnection, TrackerReference
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.routes import _dispatch_blocked_causes
from src.orchestration.state import ActorKind
from src.products.models import Product
from src.shared.models.organization import User
from tests.migrations import conftest_postgres as postgres
from tests.orchestration.product_fixture import seed_product_membership

from . import test_amendment_issue_publisher as publication
from . import test_pending_amendments_postgres as pending_postgres
from .test_amendment_proposals_postgres import proposal_app
from .test_amendment_review_routes import draft_route, permitted_policy, policied_flow_with_unbound_draft, token_context
from .test_pending_amendments import ORG_A, accepted_flow, address, amended_proposal, amender, base_proposal, open_request

factory = pending_postgres.factory
pg_server = postgres.pg_server
pg_url = postgres.pg_url
github = publication.github
dual_factory = publication.factory


async def product_epic_draft(session, *, adopted_issue=False):
    epic = publication.product_epic_proposal().origin_ref
    tracker_refs = [TrackerReference(scope="epic", address="demo-flow/epic-1", issue=epic)]
    policy = permitted_policy(repositories=("acme/api", "acme/web"))
    actor_id = amender().actor_id
    session.add(User(id=actor_id, org_id=ORG_A, team_id="team", email="epic-owner@example.test"))
    await seed_product_membership(session, org_id=ORG_A, user_id=actor_id)
    session.add(
        Product(
            team_id=f"product-team-{ORG_A}",
            id="product-1",
            tenant_id=ORG_A,
            name="Example Product",
            purpose="Test",
            intended_users=["team"],
            owner_user_id=actor_id,
            starting_point="running_product",
            created_by=actor_id,
        )
    )
    await session.flush()
    base = base_proposal(execution_policy=policy)
    flow_id = await accepted_flow(session, proposal=base)
    request = await open_request(session, flow_id)
    proposed = amended_proposal(
        extra_gate=False,
        execution_policy=policy,
        product_id="product-1",
        origin_ref=epic,
        tracker_refs=tracker_refs,
    )
    for index, (name, repo_id, installation) in enumerate((("api", 101, 22), ("web", 102, 33))):
        node = (
            next(node for node in proposed.nodes if node.address.endswith("/story-b"))
            if index == 0
            else ProposedNode(
                address=address("story-c"),
                kind="story",
                title="Build web",
            )
        )
        node.issue_ref = None
        node.proposal_identity = f"delivery:{name}"
        node.repository_identity = f"acme/{name}"
        node.provider_repository_id = repo_id
        node.repository_connection = RepositoryConnection(provider="github", installation_id=installation)
        if index == 0 and adopted_issue:
            node.issue_ref, node.proposal_identity = "8001", None
        if index:
            proposed.nodes.append(node)
            proposed.edges.append(ProposedEdge(from_address=node.address, to_address=address("eval")))
    draft = await register_amendment_draft(
        session,
        org_id=ORG_A,
        request=request,
        author_run_id="orch:author-run-1",
        proposal=proposed,
    )
    await session.commit()
    stored = await session.get(OrchestrationPendingAmendment, draft.draft_id)
    body = {
        "expected_proposal_hash": stored.proposal_hash,
        "expected_base_plan_version": stored.base_plan_version,
        "expected_base_plan_hash": stored.base_plan_hash,
    }
    return flow_id, draft.draft_id, body


async def test_human_acceptance_preserves_product_targets_and_native_parent(dual_factory, github, monkeypatch):
    publication.authorize_product_epic(monkeypatch)
    async with dual_factory() as session:
        flow_id, draft_id, body = await product_epic_draft(session)
        published = await publish_for_acceptance(
            session,
            draft_id=draft_id,
            actor=amender(),
            flow_id=flow_id,
            publisher=github.provider,
            **body,
        )
        assert [(entry.repository, entry.issue_number) for entry in published] == [("acme/api", 7000), ("acme/web", 7001)]
        accepted = await accept_amendment(session, draft_id=draft_id, actor=amender(), flow_id=flow_id, **body)
        await session.commit()
        assert accepted.plan_version == 2
    async with dual_factory() as session:
        plan = await session.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.flow_id == flow_id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
        )
        flow = await session.get(OrchestrationFlow, flow_id)
        document = LoopProposal.model_validate(plan.plan_document)
        targets = {
            node.address: (node.repository_identity, node.provider_repository_id, node.issue_ref) for node in document.nodes if node.proposal_identity
        }
        assert targets == {address("story-b"): ("acme/api", 101, "7000"), address("story-c"): ("acme/web", 102, "7001")}
        assert flow.product_id == document.product_id == "product-1"
        assert flow.origin_ref == document.origin_ref.model_dump(mode="json")
        assert flow.tracker_refs == [reference.model_dump(mode="json") for reference in document.tracker_refs]
        assert github.link_posts == 2 and github.posts == 2
        bindings = (
            await session.scalars(
                select(OrchestrationAmendmentIssueBinding).where(
                    OrchestrationAmendmentIssueBinding.org_id == ORG_A,
                )
            )
        ).all()
        assert len(bindings) == 2
        assert all(binding.parent_repository_id == 100 and binding.parent_issue_id == 8000 and binding.link_confirmed_at for binding in bindings)
        for binding in bindings:
            token = next(
                token
                for token, grant in github.tokens.items()
                if set(grant["repositories"]) == {binding.repository.split("/")[1], "planning"}
                and grant["permissions"] == {"issues": "read", "metadata": "read"}
            )
            response = await github.provider.client.get(
                f"https://api.github.com/repos/{binding.repository}/issues/{binding.issue_number}/parent",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == 200
            assert response.json() == {"id": 8000, "repository_url": "https://api.github.com/repos/acme/planning"}


async def test_unrelated_repository_publication_does_not_hold_valid_flow(dual_factory, github, monkeypatch):
    from copy import deepcopy
    from datetime import UTC, datetime

    from src.orchestration.dispatch_pass import product_binding_hold

    publication.authorize_product_epic(monkeypatch)
    monkeypatch.setenv("ADP_PRODUCT_STORY_DISPATCH_ENABLED", "true")
    async with dual_factory() as session:
        flow_id, draft_id, body = await product_epic_draft(session)
        await publish_for_acceptance(
            session,
            draft_id=draft_id,
            actor=amender(),
            flow_id=flow_id,
            publisher=github.provider,
            **body,
        )
        await accept_amendment(session, draft_id=draft_id, actor=amender(), flow_id=flow_id, **body)
        await session.commit()

    async with dual_factory() as session:
        plan = await session.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.flow_id == flow_id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
        )
        document = deepcopy(plan.plan_document)
        legacy_story = next(node for node in document["nodes"] if node["address"] == address("story-a"))
        legacy_story.update(
            repository_identity="acme/api",
            provider_repository_id=101,
            repository_connection={"provider": "github", "installation_id": 22},
        )
        plan.plan_document = document
        await session.flush()
        assert await product_binding_hold(session, org_id=ORG_A, flow_id=flow_id) is None
        session.add(
            OrchestrationAmendmentIssueBinding(
                org_id=ORG_A,
                repository="acme/unrelated",
                provider_repository_id=999,
                proposal_identity="delivery:api",
                parent_repository_id=100,
                parent_issue_id=8000,
                link_confirmed_at=datetime.now(UTC),
                state="created",
                issue_number=17001,
                claim_token="unrelated-publication",
                content_hash="different-publication",
            )
        )
        await session.flush()
        assert await product_binding_hold(session, org_id=ORG_A, flow_id=flow_id) is None
    assert github.posts == github.link_posts == 2


async def test_unconfirmed_native_parent_holds_accepted_story_dispatch(dual_factory, github, monkeypatch):
    from src.orchestration.accepted_node import accepted_node_contract
    from src.orchestration.dispatch_pass import DispatchPassConfig, DispatchPassReport, _dispatch_one, product_binding_hold
    from src.orchestration.work_admission import resolve_accepted_repository
    from src.orchestration.work_claims import WorkClaimError

    publication.authorize_product_epic(monkeypatch)
    monkeypatch.setenv("ADP_PRODUCT_STORY_DISPATCH_ENABLED", "true")
    async with dual_factory() as session:
        flow_id, draft_id, body = await product_epic_draft(session)
        await publish_for_acceptance(
            session,
            draft_id=draft_id,
            actor=amender(),
            flow_id=flow_id,
            publisher=github.provider,
            **body,
        )
        await accept_amendment(session, draft_id=draft_id, actor=amender(), flow_id=flow_id, **body)
        await session.commit()

    async with dual_factory() as session:
        assert await product_binding_hold(session, org_id=ORG_A, flow_id=flow_id) == "repository_verification_pending"
        binding = await session.scalar(
            select(OrchestrationAmendmentIssueBinding).where(
                OrchestrationAmendmentIssueBinding.proposal_identity == "delivery:api",
            )
        )
        binding.link_confirmed_at = None
        flow = await session.get(OrchestrationFlow, flow_id)
        flow.execution_paused = False
        story = await session.scalar(
            select(OrchestrationNode).where(
                OrchestrationNode.flow_id == flow_id,
                OrchestrationNode.node_ref == "story-b",
            )
        )
        story.state = "ready"
        await session.flush()
        assert await product_binding_hold(session, org_id=ORG_A, flow_id=flow_id) == "amendment_issue_link_unconfirmed"
        causes = await _dispatch_blocked_causes(session, org_id=ORG_A, flow_id=flow_id, installation_id=None)
        assert any(cause.cause == "amendment_issue_link_unconfirmed" and "native epic links" in cause.detail for cause in causes)
        with pytest.raises(WorkClaimError) as refusal:
            await resolve_accepted_repository(
                session,
                node=story,
                contract=await accepted_node_contract(session, story),
                repo="acme/api",
            )
        assert refusal.value.code == "amendment_issue_link_unconfirmed"
        report = DispatchPassReport(enabled=True)
        await _dispatch_one(session, story, config=DispatchPassConfig(queue_url="queue", repo="acme/api"), report=report)
        assert report.undispatchable == 1 and report.pending == []
        assert story.state == "ready" and story.attempts == 0
    assert github.posts == github.link_posts == 2


async def test_existing_child_parent_conflict_never_reparents_or_accepts(dual_factory, github, monkeypatch):
    publication.authorize_product_epic(monkeypatch)
    github.parents[("acme/api", 7000)] = {"id": 8001, "repository_url": "https://api.github.com/repos/acme/planning"}
    async with dual_factory() as session:
        flow_id, draft_id, body = await product_epic_draft(session)
        with pytest.raises(CycleBlockedError, match="amendment_issue_parent_conflict"):
            await publish_for_acceptance(
                session,
                draft_id=draft_id,
                actor=amender(),
                flow_id=flow_id,
                publisher=github.provider,
                **body,
            )
    assert (github.posts, github.link_posts) == (1, 0)
    assert github.parents[("acme/api", 7000)]["id"] == 8001
    async with dual_factory() as session:
        row = await session.scalar(select(OrchestrationAmendmentIssueBinding))
        assert (row.issue_number, row.parent_issue_id, row.link_confirmed_at) == (7000, 8000, None)
        with pytest.raises(ProposalRejectedError, match="missing_issue_binding"):
            await accept_amendment(session, draft_id=draft_id, actor=amender(), flow_id=flow_id, **body)
        assert await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan)) == 1
        assert await session.scalar(select(func.count()).select_from(OrchestrationNode).where(OrchestrationNode.node_ref == "story-b")) == 0


async def test_direct_amendment_cannot_reuse_unchanged_story_issues_under_another_epic(dual_factory, github, monkeypatch):
    publication.authorize_product_epic(monkeypatch)
    async with dual_factory() as session:
        flow_id, draft_id, body = await product_epic_draft(session)
        await publish_for_acceptance(
            session,
            draft_id=draft_id,
            actor=amender(),
            flow_id=flow_id,
            publisher=github.provider,
            **body,
        )
        await accept_amendment(session, draft_id=draft_id, actor=amender(), flow_id=flow_id, **body)
        await session.commit()

    async def epic(_session, *, tenant_id, name, issue, repository_id, installation_id, operation):
        assert (tenant_id, name, repository_id, installation_id) == (ORG_A, "acme/planning", 100, 11)
        return repository_binding.VerifiedRepository("github", 100, name, 11, name, issue, operation, {42: 8000, 43: 8001}[issue])

    monkeypatch.setattr(repository_binding, "resolve_story_repository", epic)
    async with dual_factory() as session:
        accepted = await session.scalar(
            select(OrchestrationAcceptedPlan).where(
                OrchestrationAcceptedPlan.flow_id == flow_id,
                OrchestrationAcceptedPlan.superseded_at.is_(None),
            )
        )
        replacement = LoopProposal.model_validate(accepted.plan_document)
        replacement.execution_policy = permitted_policy(repositories=("acme/api", "acme/web"))
        next_parent = replacement.origin_ref.model_copy(update={"issue_number": 43})
        replacement.origin_ref = next_parent
        replacement.tracker_refs = [TrackerReference(scope="epic", address="demo-flow/epic-1", issue=next_parent)]
        impact = await assess_amendment(
            session,
            org_id=ORG_A,
            flow_id=flow_id,
            proposal=replacement,
            actor_kind=ActorKind.HUMAN,
            actor_id=amender().actor_id,
        )
        assert {item.rule for item in impact.blocking} == {"amendment_issue_parent_changed"}
        with pytest.raises(ProposalRejectedError, match="amendment_issue_parent_changed"):
            await amend_plan(session, flow_id, replacement, amender())
        assert await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan)) == 2
    assert (github.posts, github.link_posts) == (2, 2)
    assert {parent["id"] for parent in github.parents.values()} == {8000}


async def test_changed_epic_issue_in_same_repository_holds_existing_child_links(dual_factory, github, monkeypatch):
    publication.authorize_product_epic(monkeypatch)
    async with dual_factory() as session:
        flow_id, draft_id, body = await product_epic_draft(session)
        await publish_for_acceptance(
            session,
            draft_id=draft_id,
            actor=amender(),
            flow_id=flow_id,
            publisher=github.provider,
            **body,
        )
    assert (github.posts, github.link_posts) == (2, 2)

    async def epic(_session, *, tenant_id, name, issue, repository_id, installation_id, operation):
        assert (tenant_id, name, repository_id, installation_id) == (ORG_A, "acme/planning", 100, 11)
        return repository_binding.VerifiedRepository("github", 100, name, 11, name, issue, operation, {42: 8000, 43: 8001}[issue])

    monkeypatch.setattr(repository_binding, "resolve_story_repository", epic)
    async with dual_factory() as session:
        draft = await session.get(OrchestrationPendingAmendment, draft_id)
        proposed = LoopProposal.model_validate(draft.proposal_document)
        next_parent = proposed.origin_ref.model_copy(update={"issue_number": 43})
        proposed.origin_ref = next_parent
        proposed.tracker_refs = [TrackerReference(scope="epic", address="demo-flow/epic-1", issue=next_parent)]
        revised = await revise_amendment(
            session,
            org_id=ORG_A,
            draft_id=draft_id,
            proposal=proposed,
            revised_by=amender().actor_id,
            flow_id=flow_id,
        )
        await session.commit()
        revised_body = {**body, "expected_proposal_hash": revised.proposal_hash}
        impact = await assess_amendment(
            session,
            org_id=ORG_A,
            flow_id=flow_id,
            proposal=proposed,
            actor_kind=ActorKind.HUMAN,
            actor_id=amender().actor_id,
        )
        assert {item.rule for item in impact.blocking} == {"amendment_issue_parent_changed"}
        assert (
            await publish_for_acceptance(
                session,
                draft_id=revised.draft_id,
                actor=amender(),
                flow_id=flow_id,
                publisher=github.provider,
                **revised_body,
            )
            == []
        )
        with pytest.raises(ProposalRejectedError, match="amendment_issue_parent_changed"):
            await accept_amendment(session, draft_id=revised.draft_id, actor=amender(), flow_id=flow_id, **revised_body)
        assert await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan)) == 1
        assert await session.scalar(select(func.count()).select_from(OrchestrationNode).where(OrchestrationNode.node_ref == "story-b")) == 0
    assert (github.posts, github.link_posts) == (2, 2)
    assert {parent["id"] for parent in github.parents.values()} == {8000}
    rows = await publication.binding_rows(dual_factory)
    assert all(row.parent_issue_id == 8000 and row.link_confirmed_at is not None for row in rows)


async def test_denied_native_link_retains_issue_and_holds_human_acceptance(dual_factory, github, monkeypatch):
    publication.authorize_product_epic(monkeypatch)
    async with dual_factory() as session:
        flow_id, draft_id, body = await product_epic_draft(session)

    github.link_status = 403
    for attempt in range(2):
        async with dual_factory() as session:
            with pytest.raises(CycleBlockedError, match="amendment_issue_link_unconfirmed"):
                await publish_for_acceptance(
                    session,
                    draft_id=draft_id,
                    actor=amender(),
                    flow_id=flow_id,
                    publisher=github.provider,
                    **body,
                )
        assert (github.posts, github.link_posts) == (1, attempt + 1)
        assert len(github.issues) == 1
        assert github.parents == {}

    async with dual_factory() as session:
        draft = await session.get(OrchestrationPendingAmendment, draft_id)
        proposal = LoopProposal.model_validate(draft.proposal_document)
        preview = await assess_amendment(
            session,
            org_id=ORG_A,
            flow_id=flow_id,
            proposal=proposal,
            actor_kind=ActorKind.HUMAN,
            actor_id=amender().actor_id,
        )
        assert [(item.repository, item.action, item.issue_number) for item in preview.issue_publications] == [
            ("acme/api", "pending_link", 7000),
            ("acme/web", "create", None),
        ]
        binding = await session.scalar(select(OrchestrationAmendmentIssueBinding))
        assert (binding.repository, binding.provider_repository_id, binding.issue_number, binding.state) == ("acme/api", 101, 7000, "created")
        assert (binding.parent_repository_id, binding.parent_issue_id, binding.link_confirmed_at) == (100, 8000, None)
        with pytest.raises(ProposalRejectedError, match="missing_issue_binding"):
            await accept_amendment(session, draft_id=draft_id, actor=amender(), flow_id=flow_id, **body)
        assert draft.state == "pending"
        assert await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan)) == 1
        assert await session.scalar(select(func.count()).select_from(OrchestrationNode).where(OrchestrationNode.node_ref == "story-b")) == 0


async def test_adopted_issue_in_a_cannot_be_proposed_as_a_story_in_b(dual_factory, github, monkeypatch):
    publication.authorize_product_epic(monkeypatch)
    async with dual_factory() as session:
        flow_id, draft_id, body = await product_epic_draft(session, adopted_issue=True)
        stored = await session.get(OrchestrationPendingAmendment, draft_id)
        proposal = LoopProposal.model_validate(stored.proposal_document)
        adopted = next(node for node in proposal.nodes if node.address == address("story-b"))
        assert adopted.issue_ref == "8001"
        adopted.repository_identity = "acme/web"
        adopted.provider_repository_id = 102
        adopted.repository_connection = RepositoryConnection(provider="github", installation_id=33)
        revised = await revise_amendment(
            session,
            org_id=ORG_A,
            draft_id=draft_id,
            proposal=proposal,
            revised_by=amender().actor_id,
            flow_id=flow_id,
        )
        await session.commit()

    calls = []

    async def verified_issue(_session, *, tenant_id, name, repository_id, issue, installation_id, operation):
        calls.append((tenant_id, name, repository_id, issue, installation_id, operation))
        assert (name, repository_id, issue, installation_id) == ("acme/web", 102, 8001, 33)
        return repository_binding.VerifiedRepository("github", 101, "acme/api", 22, "acme/api", 8001, operation, 18001)

    monkeypatch.setattr(repository_binding, "resolve_story_repository", verified_issue)
    revised_body = {**body, "expected_proposal_hash": revised.proposal_hash}
    async with dual_factory() as session:
        impact = await assess_amendment(
            session,
            org_id=ORG_A,
            flow_id=flow_id,
            proposal=proposal,
            actor_kind=ActorKind.HUMAN,
            actor_id=amender().actor_id,
        )
        assert [(item.rule, item.where) for item in impact.blocking] == [("story_issue_target_mismatch", address("story-b"))]
        assert (
            await publish_for_acceptance(
                session,
                draft_id=revised.draft_id,
                actor=amender(),
                flow_id=flow_id,
                publisher=github.provider,
                **revised_body,
            )
            == []
        )
        with pytest.raises(ProposalRejectedError, match="story_issue_target_mismatch"):
            await accept_amendment(session, draft_id=revised.draft_id, actor=amender(), flow_id=flow_id, **revised_body)
        assert await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan)) == 1
        assert await session.scalar(select(func.count()).select_from(OrchestrationAmendmentIssueBinding)) == 0
    assert calls == [(ORG_A, "acme/web", 102, 8001, 33, "preview")] * 3
    assert (github.posts, github.link_posts) == (0, 0)


@pytest.mark.parametrize("refusal", ["denied", "wrong_repository", "wrong_connection"])
async def test_adopted_issue_target_refuses_before_new_publication(dual_factory, github, monkeypatch, refusal):
    publication.authorize_product_epic(monkeypatch)

    async def verify(_session, **kwargs):
        if kwargs["name"] == "acme/planning":
            return repository_binding.VerifiedRepository("github", 100, "acme/planning", 11, "acme/planning", 42, "preview", 8000)
        assert (kwargs["name"], kwargs["issue"], kwargs["repository_id"], kwargs["installation_id"]) == ("acme/api", 8001, 101, 22)
        if refusal == "denied":
            raise repository_binding.RepositoryBindingError("repository_unavailable")
        return repository_binding.VerifiedRepository(
            "github",
            102 if refusal == "wrong_repository" else 101,
            "acme/web" if refusal == "wrong_repository" else "acme/api",
            33 if refusal == "wrong_connection" else 22,
            "acme/api",
            8001,
            "preview",
            18001,
        )

    monkeypatch.setattr(repository_binding, "resolve_story_repository", verify)
    code = "repository_unavailable" if refusal == "denied" else "story_issue_target_mismatch"
    async with dual_factory() as session:
        flow_id, draft_id, body = await product_epic_draft(session, adopted_issue=True)
        draft = await session.get(OrchestrationPendingAmendment, draft_id)
        proposal = LoopProposal.model_validate(draft.proposal_document)
        impact = await assess_amendment(
            session,
            org_id=ORG_A,
            flow_id=flow_id,
            proposal=proposal,
            actor_kind=ActorKind.HUMAN,
            actor_id=amender().actor_id,
        )
        assert any(item.rule == code and item.where == address("story-b") for item in impact.blocking)
        assert (
            await publish_for_acceptance(
                session,
                draft_id=draft_id,
                actor=amender(),
                flow_id=flow_id,
                publisher=github.provider,
                **body,
            )
            == []
        )
        with pytest.raises(ProposalRejectedError, match=code):
            await accept_amendment(session, draft_id=draft_id, actor=amender(), flow_id=flow_id, **body)
        assert await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan)) == 1
    assert (github.posts, github.link_posts) == (0, 0)


@pytest.fixture
async def published_draft(factory, github):
    async with factory() as session:
        flow_id, draft = await policied_flow_with_unbound_draft(session)
        stored = await session.get(OrchestrationPendingAmendment, draft.draft_id)
        body = {
            "expected_proposal_hash": stored.proposal_hash,
            "expected_base_plan_version": stored.base_plan_version,
            "expected_base_plan_hash": stored.base_plan_hash,
        }
        await publish_for_acceptance(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, publisher=github.provider, **body)
    return flow_id, draft, body


async def change_criteria(factory, flow_id, draft_id, criterion):
    async with factory() as session:
        stored = await session.get(OrchestrationPendingAmendment, draft_id)
        document = LoopProposal.model_validate(stored.proposal_document)
        next(node for node in document.nodes if node.proposal_identity).criteria = [criterion]
        revised = await revise_amendment(session, org_id=ORG_A, draft_id=draft_id, proposal=document, revised_by="other-human", flow_id=flow_id)
        await session.commit()
        return revised


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("status", [401, 403, 404, 410, 422, 429])
async def test_rejected_issue_write_can_retry_and_accept(dual_factory, github, monkeypatch, operation, status):
    async with dual_factory() as session:
        flow_id, draft = await policied_flow_with_unbound_draft(session)
        stored = await session.get(OrchestrationPendingAmendment, draft.draft_id)
        body = {
            "expected_proposal_hash": stored.proposal_hash,
            "expected_base_plan_version": stored.base_plan_version,
            "expected_base_plan_hash": stored.base_plan_hash,
        }
        if operation == "update":
            await publish_for_acceptance(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, publisher=github.provider, **body)

    draft = await change_criteria(dual_factory, flow_id, draft.draft_id, "recovered-criterion")
    body["expected_proposal_hash"] = draft.proposal_hash
    attempts = []
    write_issue = github.provider._write_issue

    async def record_attempt(binding, content, **kwargs):
        attempts.append(binding)
        if len(attempts) == 2:
            begin_write = binding.begin_write

            async def begin_with_stale_rejection():
                await begin_write()
                with pytest.raises(CycleBlockedError, match="amendment_issue_publication_conflict"):
                    await attempts[0].reject_write()

            binding.begin_write = begin_with_stale_rejection
        return await write_issue(binding, content, **kwargs)

    monkeypatch.setattr(github.provider, "_write_issue", record_attempt)
    github.write_status = status
    async with dual_factory() as session:
        with pytest.raises(CycleBlockedError, match="amendment_issue_provider_unavailable"):
            await publish_for_acceptance(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, publisher=github.provider, **body)
    rejected = (await publication.binding_rows(dual_factory))[0]
    assert rejected.state == "pending"
    assert publisher_module.write_identity(rejected.claim_token) != attempts[0].write_id
    assert (github.posts, github.patches) == (int(operation == "update"), 0)
    async with dual_factory() as session:
        with pytest.raises(ProposalRejectedError, match="missing_issue_binding"):
            await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, **body)

    github.write_status = None
    async with dual_factory() as session:
        await publish_for_acceptance(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, publisher=github.provider, **body)
        await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, **body)
        await session.commit()
        replay = await accept_amendment(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, **body)
        assert replay.replayed
        plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))
        work = next(node for node in LoopProposal.model_validate(plan.plan_document).nodes if node.proposal_identity)
        content = publisher_module.issue_content(work)
        assert work.criteria == ["recovered-criterion"]
    with pytest.raises(CycleBlockedError, match="amendment_issue_publication_conflict"):
        await attempts[0].begin_write()
    with pytest.raises(CycleBlockedError, match="amendment_issue_publication_conflict"):
        await attempts[0].reject_write()
    async with dual_factory() as session:
        await publish_for_acceptance(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, publisher=github.provider, **body)
    rows = await publication.binding_rows(dual_factory)
    assert len(rows) == len(github.issues) == 1
    assert rows[0].id == rejected.id
    assert rows[0].state == "created" and rows[0].content_hash == publisher_module.content_hash(content)
    assert rows[0].claim_token != rejected.claim_token
    assert publisher_module.observed_content(github.issues[0])[0]["body"] == content["body"]
    assert rows[0].claim_token not in github.issues[0]["body"]
    assert (github.posts, github.patches) == (1, int(operation == "update"))


async def test_acceptance_requires_matching_published_content(factory, published_draft):
    flow_id, draft, body = published_draft
    revised = await change_criteria(factory, flow_id, draft.draft_id, "criteria-b")
    body["expected_proposal_hash"] = revised.proposal_hash
    async with factory() as session:
        with pytest.raises(ProposalRejectedError, match="missing_issue_binding"):
            await accept_amendment(session, draft_id=revised.draft_id, actor=amender(), flow_id=flow_id, **body)
        assert await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan)) == 1


async def test_acceptance_locks_synchronized_bindings_until_commit(factory, published_draft):
    flow_id, draft, body = published_draft
    async with factory() as accepting, factory() as concurrent:
        await accept_amendment(accepting, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, **body)
        with pytest.raises(DBAPIError) as error:
            await concurrent.scalar(select(OrchestrationAmendmentIssueBinding).with_for_update(nowait=True))
        assert error.value.orig.sqlstate == "55P03"
        await concurrent.rollback()
        await accepting.commit()
        assert await concurrent.scalar(select(OrchestrationAmendmentIssueBinding).with_for_update(nowait=True)) is not None


@pytest.mark.parametrize("pause_at", ["provider_response", "before_claim"])
async def test_edit_during_publication_cannot_accept_or_overwrite_unsynchronized_content(factory, github, published_draft, monkeypatch, pause_at):
    flow_id, draft, body = published_draft
    first = await change_criteria(factory, flow_id, draft.draft_id, "criteria-a")
    first_body = {**body, "expected_proposal_hash": first.proposal_hash}
    entered, resume = asyncio.Event(), asyncio.Event()

    async def publish_with_provider(session, **kwargs):
        return await publish_for_acceptance(session, **kwargs, publisher=github.provider)

    async def pause_once():
        if not entered.is_set():
            entered.set()
            await asyncio.wait_for(resume.wait(), timeout=10)

    monkeypatch.setattr(amendment_review_routes, "publish_for_acceptance", publish_with_provider)
    if pause_at == "provider_response":
        update_issue = github.provider.update_issue

        async def delayed_update(binding, number, content):
            issue = await update_issue(binding, number, content)
            await pause_once()
            return issue

        monkeypatch.setattr(github.provider, "update_issue", delayed_update)
    else:
        publish_one = publisher_module._publish_one

        async def delayed_claim(*args, **kwargs):
            await pause_once()
            return await publish_one(*args, **kwargs)

        monkeypatch.setattr(publisher_module, "_publish_one", delayed_claim)

    app = proposal_app(factory, token_context(ORG_A))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        pending = asyncio.create_task(client.post(draft_route(flow_id, first.draft_id, "accept"), json=first_body))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            second = await change_criteria(factory, flow_id, first.draft_id, "criteria-b")
            second_body = {**body, "expected_proposal_hash": second.proposal_hash}
            response = await client.post(draft_route(flow_id, second.draft_id, "accept"), json=second_body)
            assert response.status_code == (409 if pause_at == "provider_response" else 200), response.text
            if pause_at == "provider_response":
                assert response.json()["detail"]["code"] == "amendment_issue_publication_in_progress"
        finally:
            resume.set()
            stale = await asyncio.wait_for(pending, timeout=10)
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["code"] == "draft_not_pending"
        accepted = await client.post(draft_route(flow_id, second.draft_id, "accept"), json=second_body)
        assert accepted.status_code == 200, accepted.text

    async with factory() as session:
        plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))
        work = next(node for node in LoopProposal.model_validate(plan.plan_document).nodes if node.proposal_identity)
        row = await session.scalar(select(OrchestrationAmendmentIssueBinding))
        content = publisher_module.issue_content(work)
        assert work.criteria == ["criteria-b"]
        assert row.state == "created"
        assert row.content_hash == publisher_module.content_hash(content)
        assert publisher_module.observed_content(github.issues[0])[0]["body"] == content["body"]
    assert (github.posts, github.patches) == (1, 2 if pause_at == "provider_response" else 1)


@pytest.mark.parametrize("pause_at", ["claimed", "writing"])
async def test_expired_publisher_cannot_corrupt_accepted_content(dual_factory, github, monkeypatch, pause_at):
    async with dual_factory() as session:
        flow_id, draft = await policied_flow_with_unbound_draft(session)
        stored = await session.get(OrchestrationPendingAmendment, draft.draft_id)
        body = {
            "expected_proposal_hash": stored.proposal_hash,
            "expected_base_plan_version": stored.base_plan_version,
            "expected_base_plan_hash": stored.base_plan_hash,
        }
        await publish_for_acceptance(session, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id, publisher=github.provider, **body)
    first = await change_criteria(dual_factory, flow_id, draft.draft_id, "criteria-a")

    async def publish_revision(revision):
        async with dual_factory() as session:
            return await publish_for_acceptance(
                session,
                draft_id=revision.draft_id,
                actor=amender(),
                flow_id=flow_id,
                publisher=github.provider,
                **{**body, "expected_proposal_hash": revision.proposal_hash},
            )

    entered, resume = asyncio.Event(), asyncio.Event()
    if pause_at == "claimed":
        update_issue = github.provider.update_issue

        async def delayed_update(binding, number, content):
            if not entered.is_set():
                entered.set()
                await asyncio.wait_for(resume.wait(), timeout=10)
            return await update_issue(binding, number, content)

        monkeypatch.setattr(github.provider, "update_issue", delayed_update)
    else:
        request = github.provider.request

        async def delayed_request(binding, method, path, **kwargs):
            if method == "PATCH" and not entered.is_set():
                entered.set()
                await asyncio.wait_for(resume.wait(), timeout=10)
            return await request(binding, method, path, **kwargs)

        monkeypatch.setattr(github.provider, "request", delayed_request)

    pending = asyncio.create_task(publish_revision(first))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        recovered_at = publisher_module._now() + publisher_module.CLAIM_LEASE + timedelta(seconds=1)
        monkeypatch.setattr(publisher_module, "_now", lambda: recovered_at)
        second = await change_criteria(dual_factory, flow_id, first.draft_id, "criteria-b")
        second_body = {**body, "expected_proposal_hash": second.proposal_hash}
        if pause_at == "writing":
            with pytest.raises(CycleBlockedError, match="amendment_issue_write_uncertain"):
                await publish_revision(second)
            async with dual_factory() as session:
                with pytest.raises(ProposalRejectedError, match="missing_issue_binding"):
                    await accept_amendment(session, draft_id=second.draft_id, actor=amender(), flow_id=flow_id, **second_body)
        else:
            await publish_revision(second)
            async with dual_factory() as session:
                await accept_amendment(session, draft_id=second.draft_id, actor=amender(), flow_id=flow_id, **second_body)
                await session.commit()
    finally:
        resume.set()
        if pause_at == "claimed":
            with pytest.raises(CycleBlockedError, match="amendment_issue_publication_conflict"):
                await asyncio.wait_for(pending, timeout=10)
        else:
            await asyncio.wait_for(pending, timeout=10)

    await publish_revision(second)
    async with dual_factory() as session:
        await accept_amendment(session, draft_id=second.draft_id, actor=amender(), flow_id=flow_id, **second_body)
        await session.commit()
        replay = await accept_amendment(session, draft_id=second.draft_id, actor=amender(), flow_id=flow_id, **second_body)
        assert replay.replayed
        row = await session.scalar(select(OrchestrationAmendmentIssueBinding))
        plan = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))
        work = next(node for node in LoopProposal.model_validate(plan.plan_document).nodes if node.proposal_identity)
        assert work.criteria == ["criteria-b"]
        content = publisher_module.issue_content(work)
        assert row.state == "created" and row.content_hash == publisher_module.content_hash(content)
        assert publisher_module.observed_content(github.issues[0])[0]["body"] == content["body"]
    assert (github.posts, github.patches) == (1, 1 if pause_at == "claimed" else 2)


@pytest.mark.parametrize("change", ["edit", "base", "execution"])
async def test_changed_state_after_publication_refuses_acceptance(factory, github, monkeypatch, change):
    async with factory() as session:
        flow_id, draft = await policied_flow_with_unbound_draft(session)
        base = await session.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == flow_id))
        body = {
            "expected_proposal_hash": draft.proposal_hash,
            "expected_base_plan_version": base.version,
            "expected_base_plan_hash": base.plan_hash,
        }
        original_document = base.plan_document

    async def publish_then_change(session, **kwargs):
        published = await publish_for_acceptance(session, **kwargs, publisher=github.provider)
        async with factory() as concurrent:
            if change == "edit":
                stored = await concurrent.get(OrchestrationPendingAmendment, draft.draft_id)
                proposal = LoopProposal.model_validate({**stored.proposal_document, "title": "Revised after review"})
                await revise_amendment(
                    concurrent, org_id=ORG_A, draft_id=draft.draft_id, proposal=proposal, revised_by="other-human", flow_id=flow_id
                )
            elif change == "base":
                proposal = LoopProposal.model_validate({**original_document, "title": "Another accepted plan"})
                proposal.execution_policy = proposal.execution_policy.model_copy(
                    update={"policy_id": None, "policy_hash": None, "principal_id": None}
                )
                await amend_plan(concurrent, flow_id, proposal, amender())
            else:
                await concurrent.execute(
                    update(OrchestrationNode)
                    .where(OrchestrationNode.flow_id == flow_id, OrchestrationNode.node_ref == "eval")
                    .values(state="running")
                )
            await concurrent.commit()
        return published

    monkeypatch.setattr(amendment_review_routes, "publish_for_acceptance", publish_then_change)
    app = proposal_app(factory, token_context(ORG_A))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(draft_route(flow_id, draft.draft_id, "accept"), json=body)

    assert response.status_code == (422 if change == "execution" else 409), response.text
    detail = response.json()["detail"]
    if change == "execution":
        assert "amendment_started_node_prerequisites" in {item["rule"] for item in detail["violations"]}
    else:
        assert detail["code"] == ("draft_not_pending" if change == "edit" else "stale_base")
    async with factory() as session:
        stored = await session.get(OrchestrationPendingAmendment, draft.draft_id)
        assert stored.accepted_plan_version is None
        assert await session.scalar(select(func.count()).select_from(OrchestrationAcceptedPlan)) == (2 if change == "base" else 1)
        assert await session.scalar(select(func.count()).select_from(OrchestrationNode).where(OrchestrationNode.node_ref == "story-b")) == 0
        bindings = list(await session.scalars(select(OrchestrationAmendmentIssueBinding)))
        assert [(row.state, row.issue_number) for row in bindings] == [("created", 7000)]
    assert github.posts == 1
