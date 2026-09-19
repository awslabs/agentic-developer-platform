"""Authority reads must be able to observe committed state independently."""

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.persona_models.catalogue import HARNESS_CONTRACT_REVISION
from src.agentauth.runtime_posture import reset_posture_cache
from src.shared.models.base import Base
from src.shared.models.persona_models import PersonaModelPolicySetting


@pytest.fixture
async def test_engine(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'authority.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def report_only_db(test_engine):
    """Committed rollout setting for route tests that exercise other authority gates."""
    sessions = async_sessionmaker(test_engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(
            PersonaModelPolicySetting(
                compatibility_class="claude-agent-sdk",
                harness_contract_revision=HARNESS_CONTRACT_REVISION,
                enforcement_posture="report_only",
                posture_revision=1,
                revision=1,
            )
        )
        await session.commit()
    reset_posture_cache()

    async def dependency():
        async with sessions() as session:
            yield session

    yield dependency
    reset_posture_cache()
