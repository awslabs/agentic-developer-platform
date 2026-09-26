"""Real PostgreSQL schema/role regression tests; use only a disposable server."""

import importlib.util
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
    not os.environ.get("SUPERPLANE_TEST_POSTGRES_URL")
    and importlib.util.find_spec("pgserver") is None,
    reason="requires pgserver (installed by domain CI) or a disposable PostgreSQL URL",
)


@pytest.fixture(scope="module")
def installation_postgres_url(tmp_path_factory):
    external = os.environ.get("SUPERPLANE_TEST_POSTGRES_URL")
    if external:
        yield external
        return
    import pgserver

    server = pgserver.get_server(tmp_path_factory.mktemp("superplane-installation-pg"))
    try:
        yield server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)
    finally:
        server.cleanup()


@pytest.fixture
async def isolated_database(monkeypatch, installation_postgres_url):
    url = installation_postgres_url
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


@pytest.mark.parametrize(
    "initial_head",
    [
        None,
        "017_add_workspace_bootstrap_reservations",
        "019_workspace_operation_state",
        "020_merge_workspace_cli",
        "027_cli_bootstrap_foundation",
    ],
)
async def test_full_chain_lands_only_in_owned_schema(isolated_database, initial_head):
    admin, engine, url, role, schema, foreign = isolated_database
    observed = await installation.database_check(migrating=True)
    assert observed["schema"] == schema and observed["revision"] is None
    root = Path(__file__).resolve().parents[1]
    if initial_head is not None:
        previous = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", initial_head],
            cwd=root,
            env=dict(os.environ, DATABASE_URL=url, SUPERPLANE_DB_SCHEMA=schema),
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert previous.returncode == 0, previous.stderr
    deployment_id = None
    if initial_head == "020_merge_workspace_cli":
        org_id, cluster_id, deployment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        workspace_id = uuid.uuid4()
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO organizations (id, name) VALUES (:id, 'retained-org')"
                ),
                {"id": org_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO workspaces (id, org_id, name, isolation_mode) "
                    "VALUES (:id, :org, 'retained-workspace', 'namespace')"
                ),
                {"id": workspace_id, "org": org_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO clusters (id, org_id, name) VALUES (:id, :org, 'retained-cluster')"
                ),
                {"id": cluster_id, "org": org_id},
            )
            await conn.execute(
                text(
                    "INSERT INTO deployments (id, org_id, workspace_id, cluster_id, name, status, operation_request_json) "
                    "VALUES (:id, :org, :workspace, :cluster, 'retained-workload', 'Unknown', :request)"
                ),
                {
                    "id": deployment_id,
                    "org": org_id,
                    "workspace": workspace_id,
                    "cluster": cluster_id,
                    "request": '{"name":"retained-workload"}',
                },
            )
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
    assert observed["revision"] == "042_controller_cleanup_snapshots"
    async with engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT to_regclass('workspace_bootstrap_reservations')")
            )
        ).scalar_one() is not None
        columns = (
            (
                await conn.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema=:schema AND table_name='workspaces'"
                    ),
                    {"schema": schema},
                )
            )
            .scalars()
            .all()
        )
        assert {
            "operation_id",
            "provisioning_operation_id",
            "teardown_operation_id",
        } <= set(columns)
        if deployment_id is not None:
            row = (
                await conn.execute(
                    text(
                        "SELECT name, status, operation_request_json, operation_target_json, provider_uid, workload_kind, workspace_id "
                        "FROM deployments WHERE id=:id"
                    ),
                    {"id": deployment_id},
                )
            ).one()
            assert tuple(row) == (
                "retained-workload",
                "Unknown",
                '{"name":"retained-workload"}',
                None,
                None,
                "serving",
                workspace_id,
            )
    if deployment_id is not None:
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE deployments SET workload_kind='batch' WHERE id=:id"),
                {"id": deployment_id},
            )
        rollback = subprocess.run(
            [
                sys.executable,
                "-m",
                "alembic",
                "downgrade",
                "031_controller_deployment_registry",
            ],
            cwd=root,
            env=dict(os.environ, DATABASE_URL=url, SUPERPLANE_DB_SCHEMA=schema),
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert (
            rollback.returncode != 0
            and "Retained batch records require this schema" in rollback.stderr
        )
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "042_controller_cleanup_snapshots"
            assert (
                await conn.execute(
                    text("SELECT workload_kind FROM deployments WHERE id=:id"),
                    {"id": deployment_id},
                )
            ).scalar_one() == "batch"
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO controller_batch_results "
                    "(operation_id,org_id,workspace_id,deployment_id,allocation_id,plan_digest,job_uid,pod_uid,content,sha256,redacted) "
                    "SELECT 'retained-result',org_id,workspace_id,id,'allocation',:digest,'job','pod','kept',:digest,false "
                    "FROM deployments WHERE id=:id"
                ),
                {"id": deployment_id, "digest": "a" * 64},
            )
        rollback = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "032_batch_workload_kind"],
            cwd=root,
            env=dict(os.environ, DATABASE_URL=url, SUPERPLANE_DB_SCHEMA=schema),
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert (
            rollback.returncode != 0
            and "Retained batch results require this schema" in rollback.stderr
        )
        async with engine.connect() as conn:
            assert (
                await conn.execute(text("SELECT content FROM controller_batch_results"))
            ).scalar_one() == "kept"
            assert (
                await conn.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one() == "042_controller_cleanup_snapshots"
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


async def test_control_plane_bootstrap_then_workspace_activation(bootstrap_database):
    from sqlalchemy import func, select

    from app.models.organization_grant import (
        ORGANIZATION_ADMINISTER,
        OrganizationGrantRecord,
    )
    from app.models.workspace import Workspace
    from app.models.workspace_grant import WorkspaceGrantRecord

    bootstrap, factory, config, _claims = bootstrap_database
    empty_config = {key: config[key] for key in ("org_id", "adp_org_id", "origin")}
    empty_config["control_plane_only"] = True
    first = await bootstrap.bootstrap(
        empty_config, "short-lived", membership_reader=admin_membership
    )
    assert first["workspace_id"] is None
    assert first["organization_grant"] == ORGANIZATION_ADMINISTER
    assert (
        await bootstrap.bootstrap(
            empty_config, "short-lived", membership_reader=admin_membership
        )
        == first
    )
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(OrganizationGrantRecord)
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(Workspace)) == 0
        assert (
            await session.scalar(select(func.count()).select_from(WorkspaceGrantRecord))
            == 0
        )
    await bootstrap.bootstrap(config, "short-lived", membership_reader=admin_membership)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(OrganizationGrantRecord)
            )
            == 1
        )
        assert await session.scalar(select(func.count()).select_from(Workspace)) == 1
        assert (
            await session.scalar(select(func.count()).select_from(WorkspaceGrantRecord))
            == 1
        )


async def test_revoked_org_grant_is_not_restored_by_bootstrap(bootstrap_database):
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app.models.organization_grant import OrganizationGrantRecord

    bootstrap, factory, config, _claims = bootstrap_database
    config = {key: config[key] for key in ("org_id", "adp_org_id", "origin")}
    config["control_plane_only"] = True
    await bootstrap.bootstrap(config, "short-lived", membership_reader=admin_membership)
    async with factory() as session:
        record = await session.scalar(select(OrganizationGrantRecord))
        record.revoked_at = datetime.now(timezone.utc)
        await session.commit()
    with pytest.raises(ValueError, match="organization grant is revoked"):
        await bootstrap.bootstrap(
            config, "short-lived", membership_reader=admin_membership
        )
    async with factory() as session:
        assert (
            await session.scalar(select(OrganizationGrantRecord))
        ).revoked_at is not None


@pytest.mark.parametrize(
    "mutation", ["partial-workspace", "mode-string", "membership-loss"]
)
async def test_empty_bootstrap_refuses_ambiguous_or_revoked_authority(
    bootstrap_database, mutation
):
    from sqlalchemy import func, select

    from app.models.organization_grant import OrganizationGrantRecord

    bootstrap, factory, config, _claims = bootstrap_database
    config = {key: config[key] for key in ("org_id", "adp_org_id", "origin")}
    config["control_plane_only"] = True
    if mutation == "partial-workspace":
        config["workspace_id"] = str(uuid.uuid4())
    elif mutation == "mode-string":
        config["control_plane_only"] = "false"
    calls = 0

    async def membership():
        nonlocal calls
        calls += 1
        if mutation == "membership-loss" and calls > 1:
            return {"items": []}
        return await admin_membership()

    with pytest.raises(ValueError):
        await bootstrap.bootstrap(config, "short-lived", membership_reader=membership)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(OrganizationGrantRecord)
            )
            == 0
        )


async def test_audit_migration_preserves_unattributed_evidence_on_downgrade(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, DATABASE_URL=url, SUPERPLANE_DB_SCHEMA=schema)
    upgraded = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert upgraded.returncode == 0, upgraded.stderr
    event_id = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO events (id, org_id, principal, outcome, action, resource_type, event_type) "
                "VALUES (:id, NULL, 'unresolved', 'denied', 'created', 'workspace', 'api_call')"
            ),
            {"id": event_id},
        )
    refused = subprocess.run(
        [
            sys.executable,
            "-m",
            "alembic",
            "downgrade",
            "028_deployment_namespace_quota",
        ],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert refused.returncode != 0
    async with engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT principal, outcome FROM events WHERE id=:id"),
                {"id": event_id},
            )
        ).one() == ("unresolved", "denied")
        assert (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "042_controller_cleanup_snapshots"


async def test_cluster_scopes_migrate_empty_and_enforce_tenant_foreign_keys(
    isolated_database,
):
    from sqlalchemy.exc import IntegrityError

    _, engine, url, _, schema, _ = isolated_database
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, DATABASE_URL=url, SUPERPLANE_DB_SCHEMA=schema)

    def migrate(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=root,
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr

    migrate("upgrade", "037_shared_cluster_membership")
    org_a, org_b, cluster, grant = (uuid.uuid4() for _ in range(4))
    async with engine.begin() as conn:
        for org in (org_a, org_b):
            await conn.execute(
                text("INSERT INTO organizations(id,name) VALUES (:id,:name)"),
                {"id": org, "name": "scope-org-" + org.hex},
            )
        await conn.execute(
            text(
                "INSERT INTO clusters(id,org_id,name,sharing_enabled) VALUES (:id,:org,'shared',true)"
            ),
            {"id": cluster, "org": org_a},
        )
        await conn.execute(
            text(
                "INSERT INTO organization_grants(id,org_id,principal,principal_type,permissions,granted_by) VALUES (:id,:org,'alice','human','organization:administer','fixture')"
            ),
            {"id": grant, "org": org_a},
        )
    migrate("upgrade", "head")
    async with engine.begin() as conn:
        assert (
            await conn.execute(
                text("SELECT count(*) FROM organization_grant_cluster_scopes")
            )
        ).scalar_one() == 0
        insert = text(
            "INSERT INTO organization_grant_cluster_scopes(id,org_id,grant_id,cluster_id,permissions,generation) VALUES (:id,:org,:grant,:cluster,'cluster:use','generation-1')"
        )
        # Both composite FKs must reject cross-tenant rows independently.
        foreign_grant, foreign_cluster = uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO organization_grants(id,org_id,principal,principal_type,permissions,granted_by) VALUES (:id,:org,'bob','service','','fixture')"
            ),
            {"id": foreign_grant, "org": org_b},
        )
        await conn.execute(
            text("INSERT INTO clusters(id,org_id,name) VALUES (:id,:org,'foreign')"),
            {"id": foreign_cluster, "org": org_b},
        )
        for selected_grant, selected_cluster in (
            (foreign_grant, cluster),
            (grant, foreign_cluster),
        ):
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(
                        insert,
                        {
                            "id": uuid.uuid4(),
                            "org": org_a,
                            "grant": selected_grant,
                            "cluster": selected_cluster,
                        },
                    )
        await conn.execute(
            insert,
            {"id": uuid.uuid4(), "org": org_a, "grant": grant, "cluster": cluster},
        )
        with pytest.raises(IntegrityError):
            async with conn.begin_nested():
                await conn.execute(
                    insert,
                    {
                        "id": uuid.uuid4(),
                        "org": org_a,
                        "grant": grant,
                        "cluster": cluster,
                    },
                )
    refused = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "037_shared_cluster_membership"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert refused.returncode != 0
    assert "cluster scope history" in refused.stderr
    async with engine.begin() as conn:
        assert (
            await conn.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "042_controller_cleanup_snapshots"
        assert (
            await conn.execute(
                text("SELECT count(*) FROM organization_grant_cluster_scopes")
            )
        ).scalar_one() == 1
        # Explicitly remove only this disposable fixture to exercise empty rollback.
        await conn.execute(text("DELETE FROM organization_grant_cluster_scopes"))
    migrate("downgrade", "037_shared_cluster_membership")
    async with engine.connect() as conn:
        assert (
            await conn.execute(
                text("SELECT to_regclass('organization_grant_cluster_scopes')")
            )
        ).scalar_one() is None
        assert (
            await conn.execute(
                text("SELECT permissions FROM organization_grants WHERE id=:id"),
                {"id": grant},
            )
        ).scalar_one() == "organization:administer"
