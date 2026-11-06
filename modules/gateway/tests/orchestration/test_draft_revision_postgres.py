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
from tests.orchestration.product_fixture import seed_product
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
        user_id = await reg.seed_principal(db, org_id=reg.ORG_A, role=AdminRole.ORG_ADMIN.value)
        await seed_product(db, org_id=reg.ORG_A, actor_id=reg.HUMAN_USER_ID, user_id=user_id)
        authored = reg.gateless_proposal(product_id=f"product-{reg.ORG_A}")
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
@pytest.mark.parametrize("operation", ["revision", "product"])
async def test_save_and_approval_races(database, operator, scenario, operation):  # noqa: F811
    factory, original, request, gate_id = database
    save = revision.save_draft_revision
    if operation == "product":
        async with factory() as db:
            for product_id in ("replacement-product", "competing-product"):
                await seed_product(db, org_id=reg.ORG_A, actor_id=operator.actor_id, user_id=f"user-{reg.HUMAN_USER_ID}", product_id=product_id)
            await db.commit()
        request = revision.BindDraftProductRequest(
            product_id="replacement-product",
            expected_plan_version=original.plan_version,
            expected_plan_hash=original.plan_hash,
        )
        save = revision.bind_draft_product
    loser_request = request
    if scenario == "different_save":
        if operation == "product":
            loser_request = request.model_copy(update={"product_id": "competing-product"})
        else:
            proposal = request.proposal.model_copy(update={"title": "Competing edit"})
            effective, _ = revision.transform_for_registration(proposal)
            loser_request = request.model_copy(update={"proposal": proposal, "expected_proposal_hash": revision.draft_hash(effective)})

    async def approve(db):
        return await apply_gate_answer_for_context(
            db,
            context=reg.token_context(reg.ORG_A, user_id=reg.HUMAN_USER_ID, account_type="human"),
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
                result = await approve(second) if scenario == "save_first" else await save(second, original.flow_id, loser_request, operator)
                await second.commit()
                return result
            except revision.DraftRevisionConflictError as exc:
                await second.rollback()
                return exc

        task = asyncio.create_task(contend())
        try:
            await wait_until_locked(factory, pid)
            winner = await approve(first) if scenario == "approval_first" else await save(first, original.flow_id, request, operator)
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


async def test_product_binding_survives_request_session_close(database, operator):  # noqa: F811
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.auth.dependencies import get_current_user
    from src.orchestration.draft_revision_routes import router
    from src.shared.database import get_db

    factory, original, _, gate_id = database
    async with factory() as db:
        await seed_product(db, org_id=reg.ORG_A, actor_id=operator.actor_id, user_id=f"user-{reg.HUMAN_USER_ID}", product_id="selected-product")
        await db.commit()

    async def request_db():
        async with factory() as db:
            yield db

    app = FastAPI()
    app.include_router(router, prefix="/orchestration")
    app.dependency_overrides[get_db] = request_db
    app.dependency_overrides[get_current_user] = lambda: reg.token_context(reg.ORG_A, user_id=reg.HUMAN_USER_ID, account_type="human")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        response = await http.post(
            f"/orchestration/flows/{original.flow_id}/draft/product",
            json={
                "product_id": "selected-product",
                "expected_plan_version": original.plan_version,
                "expected_plan_hash": original.plan_hash,
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["plan_version"] == 2 and not response.json()["execution_authorized"]
    # Request teardown must not roll back a save that was reported as successful.
    async with factory() as reader:
        flow = await reader.get(OrchestrationFlow, original.flow_id)
        plans = await revision.OrchestrationRepository(reader).list_plan_versions(org_id=reg.ORG_A, flow_id=original.flow_id)
        assert flow.product_id == "selected-product"
        assert len(plans) == 2 and plans[-1].plan_document["product_id"] == "selected-product"
        assert plans[-1].plan_hash == response.json()["plan_hash"]
        assert (await reg.nodes_by_ref(reader))["accept"].id == gate_id
        assert (await reg.nodes_by_ref(reader))["accept"].state == "awaiting_gate"
