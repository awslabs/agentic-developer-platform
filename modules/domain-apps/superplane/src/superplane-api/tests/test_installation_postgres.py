"""Real PostgreSQL schema/role regression tests; use only a disposable server."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app import installation

pytestmark = pytest.mark.skipif(
    not os.environ.get("SUPERPLANE_TEST_POSTGRES_URL"),
    reason="requires a disposable PostgreSQL database",
)


@pytest.fixture
async def isolated_database(monkeypatch):
    url = os.environ["SUPERPLANE_TEST_POSTGRES_URL"]
    suffix = uuid.uuid4().hex[:16]
    role, schema, foreign = "u23_role_" + suffix, "u23_" + suffix, "core_" + suffix
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE ROLE "{role}" LOGIN'))
        await conn.execute(text(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{role}"'))
        await conn.execute(text(f'ALTER ROLE "{role}" SET search_path TO "{schema}"'))
        await conn.execute(text(f'CREATE SCHEMA "{foreign}"'))
        await conn.execute(text(f'CREATE TABLE "{foreign}".sentinel (value text)'))
        await conn.execute(
            text(f"INSERT INTO \"{foreign}\".sentinel VALUES ('preserved')")
        )
    role_url = make_url(url).set(username=role).render_as_string(hide_password=False)
    engine = create_async_engine(
        role_url, connect_args={"server_settings": {"search_path": schema}}
    )
    monkeypatch.setattr(installation, "engine", engine)
    monkeypatch.setattr(installation.settings, "superplane_db_schema", schema)
    try:
        yield admin, engine, role_url, role, schema, foreign
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await conn.execute(text(f'DROP SCHEMA "{foreign}" CASCADE'))
            await conn.execute(text(f'DROP OWNED BY "{role}"'))
            await conn.execute(text(f'DROP ROLE "{role}"'))
        await admin.dispose()


async def test_full_chain_lands_only_in_owned_schema(isolated_database):
    admin, engine, url, role, schema, foreign = isolated_database
    observed = await installation.database_check(migrating=True)
    assert observed["schema"] == schema and observed["revision"] is None
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=dict(os.environ, DATABASE_URL=url, SUPERPLANE_DB_SCHEMA=schema),
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    observed = await installation.database_check(migrating=True)
    assert observed["revision"] == "015_add_adp_org_binding"
    async with admin.connect() as conn:
        assert (
            await conn.execute(text(f'SELECT value FROM "{foreign}".sentinel'))
        ).scalar_one() == "preserved"
        assert (
            await conn.execute(text("SELECT to_regclass('public.workspaces')"))
        ).scalar_one() is None
        assert (
            await conn.execute(text(f"SELECT to_regclass('{schema}.workspaces')"))
        ).scalar_one() is not None


async def test_schema_qualified_core_mutation_is_refused(isolated_database):
    admin, engine, url, role, schema, foreign = isolated_database
    async with admin.begin() as conn:
        await conn.execute(text(f'GRANT INSERT ON "{foreign}".sentinel TO "{role}"'))
    with pytest.raises(ValueError, match="another schema"):
        await installation.database_check(migrating=True)


async def test_wrong_search_path_fails_before_any_migration(
    isolated_database, monkeypatch
):
    admin, engine, url, role, schema, foreign = isolated_database
    monkeypatch.setattr(installation.settings, "superplane_db_schema", foreign)
    with pytest.raises(ValueError, match="boundary"):
        await installation.database_check(migrating=True)


async def test_raw_role_default_must_match_even_with_connection_override(
    isolated_database, monkeypatch
):
    admin, engine, url, role, schema, foreign = isolated_database
    monkeypatch.setattr(installation.settings, "database_url", url)
    assert (await installation.database_check(verify_role_default=True))[
        "schema"
    ] == schema
    async with admin.begin() as conn:
        await conn.execute(text(f'ALTER ROLE "{role}" SET search_path TO public'))
    with pytest.raises(ValueError, match="default schema"):
        await installation.database_check(verify_role_default=True)


@pytest.fixture
async def bootstrap_database(isolated_database, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from superplane_auth.policy import DomainTokenPolicy

    from app import installation_bootstrap as bootstrap
    from app.database import Base

    admin, engine, url, role, schema, foreign = isolated_database
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(bootstrap, "async_session_factory", factory)
    claims = {
        "sub": "admin-subject",
        "custom:org_id": "aws-e",
        "custom:account_type": "human",
        "token_use": "access",
        "iss": "https://issuer.example",
        "client_id": "adp-client",
    }
    monkeypatch.setattr(bootstrap, "verify_access_token", lambda token: claims)
    monkeypatch.setattr(
        bootstrap,
        "build_domain_policy",
        lambda: DomainTokenPolicy(
            allowed_client_ids=["adp-client"], expected_issuer="https://issuer.example"
        ),
    )
    config = {
        key: str(uuid.uuid4()) for key in ("org_id", "workspace_id", "cluster_id")
    }
    config.update(
        adp_org_id="aws-e",
        workspace_cluster="dedicated",
        workspace_cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/dedicated",
        workspace_namespace="domain-workspace",
        origin="https://adp.example",
    )
    return bootstrap, factory, config, claims


async def admin_membership():
    return {
        "items": [
            {
                "org_id": "aws-e",
                "name": "Example",
                "role": "org_admin",
                "is_current": True,
            }
        ]
    }


async def test_fresh_bootstrap_is_idempotent_and_revocation_is_preserved(
    bootstrap_database,
):
    from datetime import datetime, timezone

    from sqlalchemy import func, select
    from superplane_auth.policy import DomainPrincipal

    from app.auth import VerifiedCaller
    from app.models.organization import Organization
    from app.models.workspace_grant import WorkspaceGrantRecord
    from app.organization_binding import bind_caller

    bootstrap, factory, config, claims = bootstrap_database
    result = await bootstrap.bootstrap(
        config, "short-lived", membership_reader=admin_membership
    )
    assert result["org_id"] == config["org_id"] and result["actor"] == "admin-subject"
    assert (
        await bootstrap.bootstrap(
            config, "short-lived", membership_reader=admin_membership
        )
        == result
    )
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Organization)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(WorkspaceGrantRecord))
            == 1
        )
        caller = VerifiedCaller(
            DomainPrincipal("admin-subject", "aws-e", "adp-client", "human"), {}
        )
        assert (await bind_caller(session, caller)).principal.org_id == config["org_id"]
        grant = await session.scalar(select(WorkspaceGrantRecord))
        assert grant.permissions == "workspace:administer"
        grant.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    with pytest.raises(ValueError, match="revoked"):
        await bootstrap.bootstrap(
            config, "short-lived", membership_reader=admin_membership
        )


async def test_bootstrap_refuses_legacy_adoption(bootstrap_database):
    from app.models.organization import Organization

    bootstrap, factory, config, claims = bootstrap_database
    async with factory() as session:
        session.add(Organization(id=uuid.UUID(config["org_id"]), name="legacy"))
        await session.commit()
    with pytest.raises(ValueError, match="legacy adoption"):
        await bootstrap.bootstrap(
            config, "short-lived", membership_reader=admin_membership
        )


async def test_membership_revocation_before_commit_rolls_back_every_record(
    bootstrap_database,
):
    from sqlalchemy import func, select

    from app.models.organization import Organization

    bootstrap, factory, config, claims = bootstrap_database
    count = 0

    async def changing_membership():
        nonlocal count
        count += 1
        return await admin_membership() if count == 1 else {"items": []}

    with pytest.raises(ValueError, match="administrator"):
        await bootstrap.bootstrap(
            config, "short-lived", membership_reader=changing_membership
        )
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Organization)) == 0


@pytest.mark.parametrize("mutation", ["service", "wrong-org", "expired"])
async def test_bootstrap_rejects_changed_or_invalid_caller(
    bootstrap_database, monkeypatch, mutation
):
    from sqlalchemy import func, select

    from app.models.organization import Organization

    bootstrap, factory, config, claims = bootstrap_database
    if mutation == "service":
        claims["custom:account_type"] = "service"
    elif mutation == "wrong-org":
        claims["custom:org_id"] = "other"
    else:
        calls = 0

        def expires(token):
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise ValueError("expired")
            return claims

        monkeypatch.setattr(bootstrap, "verify_access_token", expires)
    with pytest.raises((ValueError, PermissionError)):
        await bootstrap.bootstrap(
            config, "short-lived", membership_reader=admin_membership
        )
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Organization)) == 0
