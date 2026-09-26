"""Real PostgreSQL replay contention uses one receipt and one policy change."""

import asyncio
from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.admin.persona_models.posture_routes import router
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.audit import AuditLog
from src.shared.models.organization import Organization, User
from src.shared.models.persona_models import PersonaModelPolicySetting
from src.shared.schemas.auth import TokenContext
from tests.admin.persona_models.test_pmm02_postgres_concurrency import (
    pg_async_url as pg_async_url,
)
from tests.admin.persona_models.test_pmm02_postgres_concurrency import (
    pg_engine as pg_engine,
)
from tests.admin.persona_models.test_pmm02_postgres_concurrency import (
    pg_server as pg_server,
)


@pytest.mark.parametrize("same_operation", [True, False])
async def test_concurrent_policy_requests_preserve_one_change(pg_engine, same_operation):  # noqa: F811
    gate = asyncio.Barrier(2)

    class RacingSession(AsyncSession):
        async def flush(self, *args, **kwargs):
            if not self.info.get("waited") and any(isinstance(row, AuditLog) and row.event_type == "persona_model_cli_operation" for row in self.new):
                self.info["waited"] = True
                await gate.wait()
            return await super().flush(*args, **kwargs)

    factory = async_sessionmaker(pg_engine, class_=RacingSession, expire_on_commit=False)
    async with factory() as db:
        db.add_all(
            [
                Organization(id="org", name="Org"),
                User(id="admin", org_id="org", team_id="team", cognito_sub="admin-sub", email="admin@example.com"),
                PersonaModelPolicySetting(compatibility_class="claude-agent-sdk", revision=1, posture_revision=1, enforcement_posture="report_only"),
            ]
        )
        await db.commit()
    app = FastAPI()
    app.include_router(router)

    async def database():
        async with factory() as db:
            yield db

    @app.exception_handler(BedrockGatewayError)
    async def handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error})

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id="admin-sub",
        org_id="org",
        team_id="team",
        department_id="",
        account_type="human",
        is_admin=True,
        auth_source="jwt",
        expires_at=date(2099, 1, 1),
    )
    body = dict(posture="enforcing", expected_revision=1, reason="same reviewed operation", operation_id=str(uuid4()))
    other_body = body if same_operation else {**body, "operation_id": str(uuid4())}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        left, right = await asyncio.wait_for(
            asyncio.gather(
                client.put("/admin/persona-models/posture/claude-agent-sdk", json=body),
                client.put("/admin/persona-models/posture/claude-agent-sdk", json=other_body),
            ),
            timeout=20,
        )
    if same_operation:
        assert left.status_code == right.status_code == 200, (left.text, right.text)
        assert left.json() == right.json()
    else:
        assert sorted([left.status_code, right.status_code]) == [200, 409], (left.text, right.text)
    async with factory() as db:
        rows = list(await db.scalars(select(AuditLog)))
        assert sum(row.event_type == "persona_model_posture_changed" for row in rows) == 1
        assert sum(row.event_type == "persona_model_cli_operation" for row in rows) == 1
        assert (await db.get(PersonaModelPolicySetting, "claude-agent-sdk")).posture_revision == 2
