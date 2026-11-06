# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Migration preserves legacy decisions and recorded separation policy."""

from sqlalchemy import text

from tests.test_organization_grant_migration import (
    installation_postgres_url as installation_postgres_url,
    isolated_database as isolated_database,
    migrate,
    pytestmark,  # noqa: F401
)


async def test_policy_migration_preserves_legacy_and_refuses_loss(isolated_database):
    _, engine, url, _, schema, _ = isolated_database
    result = migrate(url, schema, "upgrade", "044_organization_grant_changes")
    assert result.returncode == 0, result.stderr
    async with engine.begin() as connection:
        await connection.execute(
            text("""
            INSERT INTO operation_approvals
            (approval_id,org_id,workspace_id,requester,plan_digest,request_payload,
             approvers_json,max_resource_units,max_runtime_seconds,max_cost_micros,
             result,decided_by,decided_at,expires_at,revoked,organization_scope)
            VALUES ('legacy','org','workspace','requester','digest','{}','["approver"]',
                    1,60,100,'allowed-once','approver',now(),now()+interval '1 hour',false,false)
        """)
        )
    result = migrate(url, schema, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    async with engine.begin() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT requires_distinct_approver,decided_by,result FROM operation_approvals"
                )
            )
        ).one()
        assert tuple(row) == (True, "approver", "allowed-once")
    result = migrate(url, schema, "downgrade", "044_organization_grant_changes")
    assert result.returncode == 0, result.stderr
    assert migrate(url, schema, "upgrade", "head").returncode == 0
    async with engine.begin() as connection:
        await connection.execute(
            text("""
            INSERT INTO operation_approvals
            (approval_id,org_id,workspace_id,requester,plan_digest,request_payload,
             approvers_json,max_resource_units,max_runtime_seconds,max_cost_micros,
             expires_at,revoked,organization_scope,requires_distinct_approver)
            VALUES ('new','org','workspace','requester','new-digest','{}','["requester"]',
                    1,60,100,now()+interval '1 hour',false,false,false)
        """)
        )
    result = migrate(url, schema, "downgrade", "044_organization_grant_changes")
    assert result.returncode != 0
    async with engine.connect() as connection:
        assert (
            await connection.execute(
                text(
                    "SELECT requires_distinct_approver FROM operation_approvals WHERE approval_id='new'"
                )
            )
        ).scalar_one() is False
        assert (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one() == "058_interrupted_planning"
