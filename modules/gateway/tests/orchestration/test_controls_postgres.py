"""Real-PostgreSQL regression for revision-bound gate acceptance (#5331).

SQLite ignores ``SELECT ... FOR UPDATE``, so the ordinary control tests cannot
prove that an amendment is unable to commit between the plan-hash comparison and
the gate transition.  This test uses two real connections and pauses approval at
that exact boundary.  Both paths lock the same flow row; the amendment must wait
until the bound decision commits.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole
from src.orchestration.adapters.github_comments import (
    GateAnswerStatus,
    InputPath,
    apply_gate_answer_for_context,
)
from src.orchestration.amend import AmendmentContext, amend_plan
from src.orchestration.compile import ApprovalContext, compile_proposal
from src.orchestration.models import (
    DecisionKind,
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
)
from src.orchestration.proposal import LoopProposal, ProposedEdge, ProposedNode
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import NodeState
from src.shared.schemas.auth import TokenContext
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

ORG_ID = "org-revision-lock"
USER_ID = "human-approver"
FLOW_SLUG = "revision-lock"


def _address(node_ref: str, *, wave: str = "wave-1") -> str:
    return f"{FLOW_SLUG}/epic/{wave}/{node_ref}"


def _proposal(*, amended: bool = False) -> LoopProposal:
    nodes = [
        ProposedNode(address=_address("story-a"), kind="story", title="Story A"),
        ProposedNode(address=_address("eval"), kind="eval", title="Evaluation"),
        ProposedNode(address=_address("accept", wave="wave-2"), kind="gate", title="Accept"),
    ]
    edges = [
        ProposedEdge(from_address=_address("story-a"), to_address=_address("eval")),
        ProposedEdge(from_address=_address("eval"), to_address=_address("accept", wave="wave-2")),
    ]
    if amended:
        nodes.insert(1, ProposedNode(address=_address("story-b"), kind="story", title="Story B"))
        edges.insert(1, ProposedEdge(from_address=_address("story-b"), to_address=_address("eval")))
    return LoopProposal(
        flow_slug=FLOW_SLUG,
        title="Revision lock",
        org_id=ORG_ID,
        spec_revision="issue-5331",
        intent_ref="5331",
        nodes=nodes,
        edges=edges,
    )


@pytest.fixture
async def pg_engine(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url), echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(OrchestrationFlow.__table__.create)
        await conn.run_sync(OrchestrationAcceptedPlan.__table__.create)
        await conn.run_sync(OrchestrationNode.__table__.create)
        await conn.run_sync(OrchestrationEdge.__table__.create)
        await conn.run_sync(OrchestrationDecision.__table__.create)
    yield engine
    await engine.dispose()


@pytest.fixture
def pg_session_factory(pg_engine):
    return async_sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)


def _context() -> TokenContext:
    return TokenContext(
        user_id=USER_ID,
        org_id=ORG_ID,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def _access() -> AccessControl:
    access = MagicMock(spec=AccessControl)
    access.check_permission = AsyncMock(return_value=True)
    access.get_user_role = AsyncMock(return_value=(AdminRole.ORG_ADMIN, ORG_ID, None))
    return access


async def _seed(session_factory) -> tuple[str, str, str]:
    async with session_factory() as session:
        result = await compile_proposal(
            session,
            _proposal(),
            ApprovalContext(
                org_id=ORG_ID,
                actor_id=USER_ID,
                actor_role=AdminRole.ORG_ADMIN.value,
                reason="initial plan",
            ),
        )
        node = await session.get(OrchestrationNode, result.node_ids[_address("accept", wave="wave-2")])
        assert node is not None
        node.state = NodeState.AWAITING_GATE.value
        await session.commit()
        return result.flow_id, node.id, result.plan_hash


def _amender() -> AmendmentContext:
    return AmendmentContext(
        org_id=ORG_ID,
        actor_id="human-amender",
        actor_role=AdminRole.ORG_ADMIN.value,
        reason="add story B",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [True, False])
async def test_amendment_cannot_land_between_plan_read_and_gate_transition(pg_session_factory, monkeypatch, bound):
    flow_id, node_id, initial_plan_hash = await _seed(pg_session_factory)
    plan_read = asyncio.Event()
    release_approval = asyncio.Event()
    amendment_acquired_lock = asyncio.Event()

    async def approve():
        async with pg_session_factory() as session:
            real_execute = session.execute

            async def pause_after_plan_read(statement, *args, **kwargs):
                result = await real_execute(statement, *args, **kwargs)
                if "FROM orchestration_accepted_plans" in str(statement):
                    plan_read.set()
                    await asyncio.wait_for(release_approval.wait(), timeout=5)
                return result

            monkeypatch.setattr(session, "execute", pause_after_plan_read)
            outcome = await apply_gate_answer_for_context(
                session,
                context=_context(),
                node_id=node_id,
                approve=True,
                reason="reviewed plan A",
                access=_access(),
                input_path=InputPath.DASHBOARD,
                expected_plan_hash=initial_plan_hash if bound else None,
            )
            await session.commit()
            return outcome

    async def amend():
        await asyncio.wait_for(plan_read.wait(), timeout=5)
        async with pg_session_factory() as session:
            real_execute = session.execute

            async def observe_flow_lock(statement, *args, **kwargs):
                result = await real_execute(statement, *args, **kwargs)
                if "FROM orchestration_flows" in str(statement) and "FOR UPDATE" in str(statement):
                    amendment_acquired_lock.set()
                return result

            monkeypatch.setattr(session, "execute", observe_flow_lock)
            result = await amend_plan(session, flow_id, _proposal(amended=True), _amender())
            await session.commit()
            return result

    approval_task = asyncio.create_task(approve())
    await asyncio.wait_for(plan_read.wait(), timeout=5)
    amendment_task = asyncio.create_task(amend())
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(amendment_acquired_lock.wait(), timeout=0.1)
    finally:
        release_approval.set()

    outcome, amendment = await asyncio.wait_for(asyncio.gather(approval_task, amendment_task), timeout=10)
    assert outcome.status is GateAnswerStatus.APPLIED

    async with pg_session_factory() as session:
        node = await session.get(OrchestrationNode, node_id)
        current = await OrchestrationRepository(session).get_accepted_plan(org_id=ORG_ID, flow_id=flow_id)
        approvals = list(
            await session.scalars(
                select(OrchestrationDecision).where(
                    OrchestrationDecision.node_id == node_id,
                    OrchestrationDecision.kind == DecisionKind.GATE_APPROVED.value,
                )
            )
        )

    assert node is not None and node.state == NodeState.PASSED.value
    assert current is not None and current.plan_hash == amendment.plan_hash
    assert len(approvals) == 1
