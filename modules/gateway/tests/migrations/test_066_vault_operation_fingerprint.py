"""Migration guard for metadata-only vault idempotency fingerprints."""

from pathlib import Path

from sqlalchemy import create_engine, inspect

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.shared.models.vault import UserCredential
from tests.migrations.conftest_postgres import downgrade, upgrade


def test_vault_fingerprint_migration_precedes_the_single_head():
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))

    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["069_aws_verification_binding"]
    revision = scripts.get_revision("066_vault_operation_fingerprint")
    assert revision.down_revision == "066_cred_evidence_delegation"


def test_model_persists_only_a_fixed_length_fingerprint():
    column = UserCredential.__table__.c.operation_fingerprint

    assert column.nullable is True
    assert column.type.length == 64


def test_upgrade_adds_only_nullable_metadata_fingerprint(pg_url):
    upgrade(pg_url, "065_budget_enforcement_controls")
    engine = create_engine(pg_url)
    with engine.connect() as connection:
        assert "operation_fingerprint" not in {column["name"] for column in inspect(connection).get_columns("user_credentials")}

    upgrade(pg_url, "066_vault_operation_fingerprint")
    with engine.connect() as connection:
        column = next(column for column in inspect(connection).get_columns("user_credentials") if column["name"] == "operation_fingerprint")
        assert column["nullable"] is True
        assert column["type"].length == 64

    downgrade(pg_url, "065_budget_enforcement_controls")
    with engine.connect() as connection:
        assert "operation_fingerprint" not in {column["name"] for column in inspect(connection).get_columns("user_credentials")}
    engine.dispose()
