"""Exercise bot callback concurrency against PostgreSQL's real transaction locks."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.admin.connections.bot_identity import seed_bot_identity
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url

__all__ = ["pg_server", "pg_url"]


async def test_concurrent_installs_keep_one_bot_and_all_memberships(pg_url, monkeypatch):
    engine = create_async_engine(to_async_url(pg_url))
    first_projection = asyncio.Event()
    release_projection = asyncio.Event()
    projections = []
    tasks = []
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync: Base.metadata.create_all(
                    sync, tables=[model.__table__ for model in (Organization, Department, Team, User, TenantMembership, UserIdentity)]
                )
            )
            # Migration 021's production constraint is intentionally absent from
            # ORM metadata because SQLite cannot express the partial index.
            await conn.execute(text("CREATE UNIQUE INDEX one_active_membership ON tenant_memberships (user_id) WHERE is_active = true"))
        async with AsyncSession(engine) as db:
            db.add_all([Organization(id=org, name=org) for org in ("org-a", "org-b")])
            await db.commit()

        async def project(**kwargs):
            if not first_projection.is_set():
                first_projection.set()
                await release_projection.wait()
            projections.append(kwargs)
            return True

        writer = MagicMock()
        writer.put_user_identity = AsyncMock(side_effect=project)
        monkeypatch.setattr("src.admin.connections.bot_identity.IdentityIndexWriter", lambda: writer)
        github = MagicMock()
        github.get_bot_user = AsyncMock(return_value={"id": 424242, "login": "shared-platform[bot]", "type": "Bot"})

        async def install(org):
            async with AsyncSession(engine) as caller:
                await seed_bot_identity(installation_id=111, org_id=org, app_slug="shared-platform", github_client=github, db=caller)

        first = asyncio.create_task(install("org-a"))
        tasks.append(first)
        await asyncio.wait_for(first_projection.wait(), timeout=10)
        # First callback's DDB write is delayed. Let later callbacks attempt
        # both new membership creation and reinstalls before releasing it.
        tasks.extend(asyncio.create_task(install(org)) for org in ("org-b", "org-a", "org-b"))
        await asyncio.wait(tasks[1:], timeout=0.3)
        release_projection.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)

        async with AsyncSession(engine) as db:
            user = (await db.scalars(select(User))).one()
            memberships = (await db.scalars(select(TenantMembership))).all()
            assert {m.tenant_id for m in memberships} == {"org-a", "org-b"}
            assert len(memberships) == 2
            assert sum(m.is_active for m in memberships) == 1
            assert (await db.scalars(select(UserIdentity))).one().user_id == user.id
            assert len((await db.scalars(select(Department))).all()) == 1
            assert len((await db.scalars(select(Team))).all()) == 1
            assert len(projections) == 4
            assert {p["user_id"] for p in projections} == {user.id}
            assert {p["org_id"] for p in projections} == {"org-a"}
            assert projections[-1]["member_org_ids"] == ["org-a", "org-b"]
    finally:
        release_projection.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
