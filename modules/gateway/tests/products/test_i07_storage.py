"""Scoped session storage survives readback and rejects duplicate active cursors."""

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.auth.dependencies import get_current_user
from src.products.models import ProductOnboardingSession, initial_onboarding_steps
from src.shared.models.organization import Organization, User
from tests.products.membership import add_team_member
from tests.products.test_i01 import HEADERS, REQUEST


@pytest.fixture
async def member(db_session, app, valid_token_context):
    db_session.add(Organization(id=valid_token_context.org_id, name="Test workspace"))
    db_session.add(
        User(
            id=valid_token_context.user_id,
            org_id=valid_token_context.org_id,
            team_id=valid_token_context.team_id,
            email="member@example.test",
            user_kind="human",
        )
    )
    await add_team_member(db_session, valid_token_context)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: valid_token_context
    return valid_token_context


@pytest.mark.asyncio
async def test_session_storage_and_active_actor_constraint(test_client, test_engine, member):
    created = await test_client.post("/v1/products", json=REQUEST, headers=HEADERS)
    assert created.status_code == 201, created.text
    product_id, project_id = created.json()["product_id"], created.json()["project_id"]
    async with test_engine.connect() as connection:
        columns = await connection.run_sync(lambda raw: {column["name"] for column in inspect(raw).get_columns("product_operations")})
        assert "session_id" in columns
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as database:
        database.add(
            ProductOnboardingSession(
                tenant_id=member.org_id,
                product_id=product_id,
                project_id=project_id,
                actor_user_id=member.user_id,
                last_product_revision=1,
            )
        )
        await database.commit()
    async with sessions() as database:
        saved = (await database.scalars(select(ProductOnboardingSession))).one()
        assert saved.steps == initial_onboarding_steps()
        assert set(saved.steps) == {"information", "basic", "advanced", "financials"}
        assert saved.rule_version == "profile-1" and saved.last_product_revision == 1
        assert saved.proposals == [] and saved.state == "open"
        saved.steps = {**saved.steps, "financials": {"status": "later", "evidence_refs": []}}
        await database.commit()
    async with sessions() as database:
        saved = (await database.scalars(select(ProductOnboardingSession))).one()
        assert saved.steps["financials"]["status"] == "later"
        database.add(
            ProductOnboardingSession(
                tenant_id=member.org_id,
                product_id=product_id,
                project_id=project_id,
                actor_user_id=member.user_id,
                state="waiting_external",
                last_product_revision=1,
            )
        )
        with pytest.raises(IntegrityError):
            await database.commit()
        await database.rollback()
        saved.state = "completed"
        await database.commit()
        database.add(
            ProductOnboardingSession(
                tenant_id=member.org_id,
                product_id=product_id,
                project_id=project_id,
                actor_user_id=member.user_id,
                last_product_revision=1,
            )
        )
        await database.commit()
    async with sessions() as database:
        assert len((await database.scalars(select(ProductOnboardingSession))).all()) == 2
