"""The vault migration extends the current chain and preserves shared records."""

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, upgrade


def test_vault_migration_is_on_the_single_head_chain():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert "066_cred_evidence_delegation" in {revision.revision for revision in script.walk_revisions()}
    assert script.get_revision("066_cred_evidence_delegation").down_revision == "065_budget_enforcement_controls"


def test_current_database_upgrades_and_vault_downgrade_preserves_controls(pg_url):
    upgrade(pg_url, "065_budget_enforcement_controls")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO budget_enforcement_settings VALUES ('global', false, 1, 'operator', NOW())"))
        upgrade(pg_url, "066_cred_evidence_delegation")
        with engine.connect() as connection:
            tables = inspect(connection).get_table_names()
            assert {"credential_workspace_delegations", "credential_validation_evidence"} <= set(tables)
            assert "validated_version_id" in {column["name"] for column in inspect(connection).get_columns("credential_validation_evidence")}
            assert "provider_account_id" in {column["name"] for column in inspect(connection).get_columns("credential_validation_evidence")}
            assert connection.scalar(text("SELECT enabled FROM budget_enforcement_settings WHERE scope_key='global'")) is False
        downgrade(pg_url, "065_budget_enforcement_controls")
        with engine.connect() as connection:
            assert "credential_workspace_delegations" not in inspect(connection).get_table_names()
            assert "credential_validation_evidence" not in inspect(connection).get_table_names()
            assert connection.scalar(text("SELECT enabled FROM budget_enforcement_settings WHERE scope_key='global'")) is False
    finally:
        engine.dispose()
