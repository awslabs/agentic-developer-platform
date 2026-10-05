"""Draft saves serialize with real gate answers and other saves in PostgreSQL."""

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole
from src.orchestration import draft_revision as revision
from src.orchestration.adapters.github_comments import InputPath, apply_gate_answer_for_context
from src.orchestration.models import OrchestrationFlow
from src.orchestration.registration import register_draft_proposal
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration import test_registration as reg
from tests.orchestration.test_draft_revision import operator, request_for  # noqa: F401

pytestmark = pytest.mark.integration
registrar = reg.registrar
autonomy_default_unset = reg.autonomy_default_unset
provider_repository_identity = reg.provider_repository_identity


@pytest.fixture
async def database(pg_url, registrar, operator):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as connection:
        await connection.run_sync(reg.Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await reg.seed_org(db)
        await reg.seed_principal(db, org_id=reg.ORG_A, role=AdminRole.ORG_ADMIN.value)
        authored = reg.gateless_proposal()
        result, _ = await register_draft_proposal(db, authored, registrar)
        await db.commit()
        request = request_for((result, authored))
        preview = await revision.preview_draft_revision(db, result.flow_id, request, operator)
        write = revision.SaveDraftRevisionRequest(**request.model_dump(), expected_proposal_hash=preview["proposal_hash"])
    yield factory, result, write, preview["acceptance_gate_id"]
    await engine.dispose()


async def wait_until_locked(factory, pid):
    """Prove the contender reached PostgreSQL and actually waited on the lock."""
    async with asyncio.timeout(5), factory() as db:
        while await db.scalar(text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}) != "Lock":
            await db.commit()  # refresh pg_stat_activity's transaction snapshot
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("scenario", ["same_save", "different_save", "approval_first", "save_first"])
async def test_save_and_approval_races(database, operator, scenario):  # noqa: F811
    factory, original, request, gate_id = database
    loser_request = request
    if scenario == "different_save":
        proposal = request.proposal.model_copy(update={"title": "Competing edit"})
        effective, _ = revision.transform_for_registration(proposal)
        loser_request = request.model_copy(update={"proposal": proposal, "expected_proposal_hash": revision.draft_hash(effective)})

    async def approve(db):
        return await apply_gate_answer_for_context(
            db,
            context=reg.token_context(reg.ORG_A, user_id=reg.HUMAN_USER_ID),
            node_id=gate_id,
            approve=True,
            reason="Review original draft",
            access=AccessControl(db),
            input_path=InputPath.DASHBOARD,
            expected_plan_hash=original.plan_hash,
        )

    async with factory() as first, factory() as second:
        # The first operation owns the same lock its public service takes. Force
        # a contender to wait before the winner writes; this cannot accidentally
        # pass by running the two operations sequentially.
        await first.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == original.flow_id).with_for_update())
        pid = await second.scalar(text("SELECT pg_backend_pid()"))

        async def contend():
            try:
                result = (
                    await approve(second)
                    if scenario == "save_first"
                    else await revision.save_draft_revision(second, original.flow_id, loser_request, operator)
                )
                await second.commit()
                return result
            except revision.DraftRevisionConflictError as exc:
                await second.rollback()
                return exc

        task = asyncio.create_task(contend())
        try:
            await wait_until_locked(factory, pid)
            winner = (
                await approve(first)
                if scenario == "approval_first"
                else await revision.save_draft_revision(first, original.flow_id, request, operator)
            )
            await first.commit()
            loser = await asyncio.wait_for(task, timeout=5)
        finally:
            await first.rollback()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async with factory() as db:
        plans = await revision.OrchestrationRepository(db).list_plan_versions(org_id=reg.ORG_A, flow_id=original.flow_id)
        gate = (await reg.nodes_by_ref(db))["accept"]
        if scenario == "approval_first":
            assert winner.status.value == "applied"
            assert isinstance(loser, revision.DraftRevisionConflictError) and loser.code == "draft_already_approved"
            assert len(plans) == 1 and gate.state == "passed"
        else:
            assert winner["plan_version"] == 2 and len(plans) == 2
            assert gate.state == "awaiting_gate"
            if scenario == "same_save":
                assert loser["already_revised"] and loser["plan_version"] == 2
            elif scenario == "different_save":
                assert isinstance(loser, revision.DraftRevisionConflictError) and loser.code == "stale_draft_revision"
            else:
                assert loser.status.value == "refused_stale_plan"
