"""A removed canonical membership must not revive during schema rollback."""

import psycopg2

from tests.migrations.conftest_postgres import run_alembic, upgrade


def test_revocation_tombstone_survives_refused_downgrade(pg_url):
    upgrade(pg_url, "head")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("INSERT INTO organizations (id,name,aws_accounts,role_mappings,settings,created_at) VALUES ('revoked-org','Revoked org','[]','{}','{}',NOW())")
        cursor.execute("INSERT INTO users (id,org_id,team_id,email,created_at) VALUES ('revoked-user','revoked-org','','removed@example.test',NOW())")
        cursor.execute(
            "INSERT INTO tenant_memberships (id,user_id,tenant_id,role,revoked_at) VALUES ('revoked-member','revoked-user','revoked-org','member',NOW())"
        )
    result = run_alembic(pg_url, "downgrade", "074_budget_settlement_receipts")
    assert result.returncode != 0
    assert "Membership revocation tombstones must survive rollback" in result.stdout + result.stderr
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT revoked_at IS NOT NULL FROM tenant_memberships WHERE id='revoked-member'")
        assert cursor.fetchone() == (True,)
