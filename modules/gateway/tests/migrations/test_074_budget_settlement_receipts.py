"""The current migration chain installs receipts and refuses destructive rollback."""

import psycopg2
import pytest

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import HEAD_DOWNGRADE_REFUSAL


@pytest.mark.parametrize(
    "starting_revision,barrier",
    [
        ("080_usage_request_index", "Settlement receipts must survive rollback"),
        ("119_metering_binding_fence", "Metering identity and membership fences must survive rollback"),
        ("head", HEAD_DOWNGRADE_REFUSAL),
    ],
)
def test_current_head_installs_and_preserves_settlement_receipts(pg_url, starting_revision, barrier):
    upgrade(pg_url, starting_revision)
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
    assert barrier in result.stdout + result.stderr
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        # Migration 080 commits earlier downgrade steps before its concurrent
        # index operation. The later refusal preserves debit receipts, but does
        # not undo those already committed steps.
        assert cursor.fetchone()[0] == (expected_head if starting_revision == "head" else starting_revision)
        cursor.execute("SELECT request_id,total_tokens FROM budget_settlement_receipts")
        assert cursor.fetchall() == [("request", 6)]
    # A partially committed rollback remains safe to upgrade again.
    upgrade(pg_url, "head")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone()[0] == expected_head
        cursor.execute("SELECT request_id,total_tokens FROM budget_settlement_receipts")
        assert cursor.fetchall() == [("request", 6)]
