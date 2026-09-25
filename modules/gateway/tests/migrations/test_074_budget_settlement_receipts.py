"""The current migration chain installs receipts and refuses destructive rollback."""

import psycopg2

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade


def test_current_head_installs_and_preserves_settlement_receipts(pg_url):
    upgrade(pg_url, "head")
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO budget_settlement_receipts "
            "(org_id,request_id,user_id,cost_usd,total_tokens,allocation_key) "
            "VALUES ('tenant','request','owner',0.01,6,%s)",
            ("a" * 64,),
        )
    result = run_alembic(pg_url, "downgrade", "073_kimi_k3_pricing")
    assert result.returncode != 0
    assert "Settlement receipts must survive rollback" in result.stdout + result.stderr
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone()[0] == expected_head
        cursor.execute("SELECT request_id,total_tokens FROM budget_settlement_receipts")
        assert cursor.fetchall() == [("request", 6)]
    # A refused rollback leaves the schema/version consistent and re-upgrade safe.
    upgrade(pg_url, "head")
