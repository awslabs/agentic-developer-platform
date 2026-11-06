"""Pending proposals survive a gateway reconnect without releasing execution."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole
from src.auth.dependencies import get_current_user
from src.orchestration import amendment_review_routes, draft_routes
from src.orchestration.authoring_dispatch import build_authoring_assignment
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationAmendmentIssueBinding,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPendingAmendment,
    OrchestrationWorkClaim,
)
from src.orchestration.pending_amendments import owed_authoring_requests, register_amendment_draft, resolve_authoring_request
from src.shared.database import get_db
from tests.migrations import conftest_postgres as postgres

from . import test_pending_amendments_postgres as pending_postgres
from .test_amendment_draft_route import token_context as agent_token
from .test_amendment_review_routes import HUMAN_USER_ID, draft_route, drafts_route, insert_after_body, proposals_route, refuse_publication
from .test_amendment_review_routes import token_context as human_token
from .test_pending_amendments import AUTHOR_RUN, ORG_A, accepted_flow, address, amended_proposal, open_request

factory = pending_postgres.factory
pg_server = postgres.pg_server
pg_url = postgres.pg_url


def proposal_app(session_factory, principal):
    app = FastAPI()
    app.include_router(amendment_review_routes.router, prefix="/orchestration")
    app.include_router(draft_routes.router)

    async def request_session():
        async with session_factory() as session:
            yield session

    access = MagicMock(spec=AccessControl)
    access.check_permission = AsyncMock(return_value=True)
    role = AdminRole.ORG_ADMIN if principal.account_type == "human" else AdminRole.MEMBER
    access.get_user_role = AsyncMock(return_value=(role, ORG_A, None))
    bindings = MagicMock()
    bindings.resolve = AsyncMock(side_effect=AssertionError("tenant-authenticated callers need no external binding lookup"))
    app.dependency_overrides[get_db] = request_session
    app.dependency_overrides[get_current_user] = lambda: principal
    app.dependency_overrides[amendment_review_routes.get_access_control] = lambda: access
    app.dependency_overrides[draft_routes.get_access_control] = lambda: access
    app.dependency_overrides[draft_routes.get_run_binding_resolver] = lambda: bindings
    return app


async def execution_snapshot(session):
    snapshot = {}
    for model in (
        OrchestrationFlow,
        OrchestrationNode,
        OrchestrationEdge,
        OrchestrationAcceptedPlan,
        OrchestrationWorkClaim,
        OrchestrationExecution,
        OrchestrationAction,
        OrchestrationAmendmentIssueBinding,
    ):
        snapshot[model.__tablename__] = (await session.execute(select(model.__table__).order_by(model.id))).mappings().all()
    snapshot["decisions"] = (
        (
            await session.execute(
                select(OrchestrationDecision.__table__)
                .where(OrchestrationDecision.kind != DecisionKind.REPLAN_REQUESTED.value)
                .order_by(OrchestrationDecision.id)
            )
        )
        .mappings()
        .all()
    )
    return snapshot


@pytest.mark.parametrize("origin", ["human", "agent"])
async def test_proposals_survive_reconnect_without_execution_or_reauthoring(factory, pg_url, monkeypatch, origin):
    refuse_publication(monkeypatch)
    async with factory() as session:
        flow_id = await accepted_flow(session)
        await session.execute(update(OrchestrationNode).where(OrchestrationNode.node_ref == "story-a").values(state="passed", attempts=2))
        await session.commit()
        if origin == "agent":
            request = await open_request(session, flow_id)
            principal = agent_token(ORG_A)
            path = drafts_route(flow_id)
            post_args = {
                "params": {"request_id": request.id},
                "headers": {"X-Agent-RunId": AUTHOR_RUN},
                "json": amended_proposal().model_dump(mode="json"),
            }
            expected_author = AUTHOR_RUN
            expected_requester = request.requested_by
        else:
            principal = human_token(ORG_A)
            path = proposals_route(flow_id)
            post_args = {"json": insert_after_body(address("story-a"))}
            expected_author = f"operator:{HUMAN_USER_ID}"
            expected_requester = HUMAN_USER_ID
        before = await execution_snapshot(session)

    app = proposal_app(factory, principal)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, **post_args)
    assert response.status_code == 201, response.text
    created = response.json()

    async with factory() as session:
        draft = (await session.execute(select(OrchestrationPendingAmendment))).scalar_one()
        assert draft.id == created["draft_id"]
        assert draft.state == "pending"
        assert draft.author_run_id == expected_author
        assert draft.base_plan_version == 1
        assert draft.accepted_by_decision_id is None
        document = draft.proposal_document
        base_hash = draft.base_plan_hash
        assignment = await resolve_authoring_request(session, org_id=ORG_A, request_id=draft.request_id, author_run_id=expected_author)
        assert assignment.requested_by == expected_requester
        assert await execution_snapshot(session) == before
        unfinished = await open_request(session, flow_id, decision_id="unfinished-request", run_id="orch:unfinished")

    await factory.kw["bind"].dispose()
    restarted_engine = create_async_engine(postgres.to_async_url(pg_url))
    restarted_factory = async_sessionmaker(restarted_engine, expire_on_commit=False)
    try:
        restarted_app = proposal_app(restarted_factory, human_token(ORG_A))
        async with AsyncClient(transport=ASGITransport(app=restarted_app), base_url="http://test") as client:
            listing = await client.get(drafts_route(flow_id))
            detail = await client.get(draft_route(flow_id, created["draft_id"]))
        assert listing.status_code == detail.status_code == 200
        assert [entry["draft_id"] for entry in listing.json()] == [created["draft_id"]]
        persisted = detail.json()
        assert persisted["proposal_hash"] == created["proposal_hash"]
        assert persisted["proposal_document"] == document
        assert persisted["base_plan_hash"] == base_hash
        assert persisted["base_plan_version"] == 1
        assert persisted["base_is_current"] is True
        assert persisted["state"] == "pending"
        assert persisted["requested_by"] == expected_requester

        retry_app = proposal_app(restarted_factory, principal)
        async with AsyncClient(transport=ASGITransport(app=retry_app), base_url="http://test") as client:
            repeated = await client.post(path, **post_args)
        assert repeated.status_code in {200, 201}, repeated.text
        assert repeated.json()["draft_id"] == created["draft_id"]

        async with restarted_factory() as session:
            assert await execution_snapshot(session) == before
            stored = (await session.scalars(select(OrchestrationPendingAmendment))).one()
            assert stored.author_run_id == expected_author
            assert stored.request_id == assignment.id
            owed = await owed_authoring_requests(session, limit=1, older_than=datetime.now(UTC) + timedelta(minutes=1))
            assert [request.id for _, request, _ in owed] == [unfinished.id]
            rebuilt = await build_authoring_assignment(
                session, org_id=ORG_A, request=assignment, repo="example/project", issue=5329, installation_id=123
            )
            assert rebuilt is None
    finally:
        await restarted_engine.dispose()


async def test_rolled_back_registration_leaves_authoring_recoverable(factory):
    async with factory() as session:
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        await register_amendment_draft(session, org_id=ORG_A, request=request, author_run_id=AUTHOR_RUN, proposal=amended_proposal())
        await session.rollback()

    async with factory() as session:
        assert (await session.scalars(select(OrchestrationPendingAmendment))).all() == []
        owed = await owed_authoring_requests(session, limit=1, older_than=datetime.now(UTC) + timedelta(minutes=1))
        assert [assignment.id for _, assignment, _ in owed] == [request.id]
