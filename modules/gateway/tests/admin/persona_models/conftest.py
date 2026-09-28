"""Test harness for persona-model catalogue — Issue #5420 (PMM-03).

Mirrors the bedrock_routing test pattern: SQLite in-memory database, fixture-based
seeding, and real service logic (no mocking of gates).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.admin.persona_models.catalogue_routes import router as persona_models_router
from src.admin.persona_models.catalogue_service import compute_request_shape_sha256
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.base import Base
from src.shared.models.persona_model_catalogue import ModelInvocabilityEvidence
from src.shared.schemas.auth import TokenContext

ORG_ID = "org-5420-acme"
MEMBER_SUB = "sub-5420-member"
MEMBER_ID = "54200000-0000-4000-8000-000000000001"

# Destination for evidence tests
DEST_ACCOUNT = "111111115420"
DEST_REGION = "us-east-1"

# Default canonical model for evidence tests
DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"


@pytest.fixture
async def engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
def new_session(engine):
    """Open an *additional*, independent session on the same engine.

    Writing and reading through one session is not a faithful test of a
    persisted row: SQLAlchemy's identity map returns the very same Python
    object that was added, so column values keep whatever types they had in
    memory and never make the round trip through the database.

    Production never behaves that way — a probe records evidence in one
    request and a later request reads it back in a fresh session.  Tests that
    need to assert on *stored* representation (timezone awareness in
    particular) must use this fixture so the row is genuinely re-loaded.

    Usage::

        async with new_session() as s:
            row = await s.get(ModelInvocabilityEvidence, key)
    """
    return async_sessionmaker(engine, expire_on_commit=False)


def member_context(*, org_id: str = ORG_ID) -> TokenContext:
    return TokenContext(
        user_id=MEMBER_SUB,
        org_id=org_id,
        team_id="team-5420-ml",
        department_id="",
        account_type="human",
        is_admin=False,
        expires_at=date(2099, 1, 1),
    )


def build_app(session: AsyncSession, context: TokenContext | None = None) -> FastAPI:
    """Mount the persona-models router with overridden dependencies."""
    app = FastAPI()
    app.include_router(persona_models_router)

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    if context is not None:
        app.dependency_overrides[get_current_user] = lambda: context
    return app


def client_for(session: AsyncSession, context: TokenContext | None = None) -> AsyncClient:
    app = build_app(session, context)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def make_evidence(
    *,
    account_id: str = DEST_ACCOUNT,
    region: str = DEST_REGION,
    canonical_model_id: str = DEFAULT_MODEL_ID,
    outcome: str = "proven",
    compatibility_class: str = "claude-agent-sdk",
    harness_contract_revision: str = "0.3.283",
    request_shape_sha256: str | None = None,
    error_code: str | None = None,
    provider_request_id: str | None = "req-12345",
    verified_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ModelInvocabilityEvidence:
    """Create an evidence row for testing.

    Computes the canonical request_shape_sha256 by default so evidence
    matches the full 6-column primary key that the service uses.
    """
    now = datetime.now(UTC)
    # Compute the canonical request shape SHA-256 if not overridden.
    if request_shape_sha256 is None:
        request_shape_sha256 = compute_request_shape_sha256(canonical_model_id)
    return ModelInvocabilityEvidence(
        account_id=account_id,
        region=region,
        canonical_model_id=canonical_model_id,
        compatibility_class=compatibility_class,
        harness_contract_revision=harness_contract_revision,
        request_shape_sha256=request_shape_sha256,
        outcome=outcome,
        error_code=error_code,
        provider_request_id=provider_request_id,
        verified_at=verified_at or now,
        expires_at=expires_at or (now + timedelta(hours=24)),
        updated_at=now,
    )
