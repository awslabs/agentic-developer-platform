"""The release migration works from the previous production revision."""

from sqlalchemy import create_engine, inspect, text

from tests.migrations.conftest_postgres import downgrade, upgrade


def test_upgrade_preserves_usage_and_downgrade_removes_only_controls(pg_url):
    upgrade(pg_url, "064_orchestration_run_reports")
    engine = create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE budget_migration_sentinel (amount NUMERIC NOT NULL)"))
        conn.execute(text("INSERT INTO budget_migration_sentinel VALUES (69.60)"))
    upgrade(pg_url, "065_budget_enforcement_controls")
    with engine.begin() as conn:
        assert "budget_enforcement_settings" in inspect(conn).get_table_names()
        assert "budget_accounting_gaps" in inspect(conn).get_table_names()
        conn.execute(text("INSERT INTO budget_enforcement_settings VALUES ('global', false, 1, 'operator', NOW())"))
        conn.execute(text("INSERT INTO budget_accounting_gaps VALUES ('missing', 'global', NOW())"))
        assert conn.scalar(text("SELECT enabled FROM budget_enforcement_settings")) is False
        assert str(conn.scalar(text("SELECT amount FROM budget_migration_sentinel"))) == "69.60"
    downgrade(pg_url, "064_orchestration_run_reports")
    with engine.connect() as conn:
        assert "budget_enforcement_settings" not in inspect(conn).get_table_names()
        assert conn.scalar(text("SELECT amount FROM budget_migration_sentinel")) > 0
    engine.dispose()
