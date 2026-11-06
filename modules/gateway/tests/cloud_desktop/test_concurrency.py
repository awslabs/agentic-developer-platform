"""The policy-row lock serializes competing owners at the final quota slot."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.auth.middleware import get_current_user_context
from src.cloud_desktop.routes import router
from src.shared.database import get_db
from src.shared.models.dev_box import DevBox, DevBoxPolicy, DevBoxTemplate
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext
from tests.migrations.conftest_postgres import to_async_url, upgrade

pytest_plugins = ("tests.migrations.conftest_postgres",)


@pytest.mark.asyncio
async def test_concurrent_setup_never_exceeds_one_live_slot(pg_url, monkeypatch):
    upgrade(pg_url, "head")
    monkeypatch.setenv("BG_DEV_BOX_DEPLOYMENT_ID", "dev")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_LIVE_BOXES", "1")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_BOXES", "1")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_STORAGE_GB", "100")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_RUNNING_HOURS", "100")
    monkeypatch.setenv("BG_DEV_BOX_MAX_DEPLOYMENT_BUDGET_USD", "100")
    engine = create_async_engine(to_async_url(pg_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as db:
            db.add(Organization(id="org-box", name="Box Org", settings={"crew": {"allowed_targets": ["ec2-instance"]}}))
            db.add_all(
                [
                    User(id=identity, org_id="org-box", team_id="team", email=f"{identity}@example.test", cognito_sub=identity)
                    for identity in ("alice", "bob")
                ]
            )
            await db.flush()
            db.add_all([TenantMembership(user_id=identity, tenant_id="org-box") for identity in ("alice", "bob")])
            db.add_all(
                [
                    DevBoxPolicy(
                        id="policy",
                        org_id="org-box",
                        deployment_id="dev",
                        enabled=True,
                        max_live_boxes=1,
                        max_running_boxes=1,
                        allowed_sizes=["small"],
                        max_disk_gb=40,
                        max_storage_gb=100,
                        max_running_hours=100,
                        instance_hour_rate_usd=Decimal("0.10"),
                        disk_gb_hour_rate_usd=Decimal("0.0001"),
                        snapshot_gb_hour_rate_usd=Decimal("0.0001"),
                        budget_usd=Decimal("50"),
                        max_lease_minutes=60,
                    ),
                    DevBoxTemplate(id="template", name="assistant", revision="r1", profile="crew", instance_size="small", disk_gb=30, approved=True),
                ]
            )
            await db.commit()

        app = FastAPI()
        app.include_router(router)

        async def get_session():
            async with factory() as session:
                yield session

        async def get_caller(request: Request):
            return TokenContext(
                user_id=request.headers["X-Test-Owner"],
                org_id="org-box",
                team_id="team",
                department_id="dept",
                account_type="human",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        app.dependency_overrides[get_db] = get_session
        app.dependency_overrides[get_current_user_context] = get_caller
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            responses = await asyncio.gather(
                *[
                    http.post(
                        "/cloud-desktops",
                        headers={"X-Test-Owner": identity, "Idempotency-Key": f"setup-{identity}"},
                        json={"template": "assistant", "profile": "crew"},
                    )
                    for identity in ("alice", "bob")
                ]
            )
        assert sorted(response.status_code for response in responses) == [202, 403], [response.text for response in responses]
        async with factory() as db:
            assert await db.scalar(select(func.count(DevBox.id))) == 1
    finally:
        await engine.dispose()
