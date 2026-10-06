"""Organization revision upgrade and evidence-preserving downgrade on PostgreSQL."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests import test_installation_postgres as installation_fixtures

installation_postgres_url = installation_fixtures.installation_postgres_url
isolated_database = installation_fixtures.isolated_database
pytestmark = installation_fixtures.pytestmark


def migrate(url, schema, command, revision):
    return subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=Path(__file__).resolve().parents[1],
        env=dict(os.environ, DATABASE_URL=url, SUPERPLANE_DB_SCHEMA=schema),
        text=True,
        capture_output=True,
        timeout=60,
    )


async def test_organization_revision_upgrade_retains_revocations_and_audit(
    isolated_database,
):
    _, engine, url, _, schema, _ = isolated_database
    initial = migrate(url, schema, "upgrade", "043_workspace_grant_changes")
    assert initial.returncode == 0, initial.stderr
    org_id, foreign_id, live_id, revoked_id, event_id, request_id = [
        uuid.uuid4() for _ in range(6)
    ]
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO organizations(id, name) VALUES (:org, 'organization'), (:foreign, 'foreign')"
            ),
            {"org": org_id, "foreign": foreign_id},
        )
        await connection.execute(
            text("""INSERT INTO organization_grants(id, org_id, principal, principal_type, permissions, granted_by, revoked_at)
            VALUES (:live, :org, 'live', 'human', 'organization:administer', 'fixture', NULL),
                   (:revoked, :org, 'revoked', 'human', 'organization:read', 'fixture', now())"""),
            {"live": live_id, "revoked": revoked_id, "org": org_id},
        )
        revoked_at = (
            await connection.execute(
                text("SELECT revoked_at FROM organization_grants WHERE id=:id"),
                {"id": revoked_id},
            )
        ).scalar_one()
    for _attempt in range(2):
        result = migrate(url, schema, "upgrade", "head")
        assert result.returncode == 0, result.stderr
    async with engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "044_organization_grant_changes"
        rows = (
            await connection.execute(
                text(
                    "SELECT id, revision, revoked_at, permissions FROM organization_grants"
                )
            )
        ).all()
        assert {
            row.id: (row.revision, row.revoked_at, row.permissions) for row in rows
        } == {
            live_id: (1, None, "organization:administer"),
            revoked_id: (1, revoked_at, "organization:read"),
        }
    empty_downgrade = migrate(url, schema, "downgrade", "043_workspace_grant_changes")
    assert empty_downgrade.returncode == 0, empty_downgrade.stderr
    assert migrate(url, schema, "upgrade", "head").returncode == 0
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO events(id, org_id, principal, action, resource_type, resource_id, event_type) VALUES (:event, :org, 'actor', 'revoked', 'organization_grant', :grant, 'organization_access')"
            ),
            {"event": event_id, "org": org_id, "grant": live_id},
        )
        statement = text(
            "INSERT INTO organization_grant_changes(id, org_id, request_id, grant_id, event_id, fingerprint, revision) VALUES (:id, :org, :request, :grant, :event, :fingerprint, 2)"
        )
        values = {
            "id": uuid.uuid4(),
            "org": foreign_id,
            "request": request_id,
            "grant": live_id,
            "event": event_id,
            "fingerprint": "a" * 64,
        }
        with pytest.raises(IntegrityError):
            async with connection.begin_nested():
                await connection.execute(statement, values)
        await connection.execute(statement, values | {"org": org_id})
        await connection.execute(
            text(
                "UPDATE organization_grants SET revision=2, revoked_at=now() WHERE id=:id"
            ),
            {"id": live_id},
        )
    refused = migrate(url, schema, "downgrade", "043_workspace_grant_changes")
    assert (
        refused.returncode != 0
        and "organization grant change evidence must be retained" in refused.stderr
    )
    await engine.dispose()
    async with engine.connect() as connection:
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "044_organization_grant_changes"
        assert (
            await connection.execute(
                text("SELECT count(*) FROM organization_grant_changes")
            )
        ).scalar_one() == 1
        assert (
            await connection.execute(
                text(
                    "SELECT revision, revoked_at IS NOT NULL FROM organization_grants WHERE id=:id"
                ),
                {"id": live_id},
            )
        ).one() == (2, True)
