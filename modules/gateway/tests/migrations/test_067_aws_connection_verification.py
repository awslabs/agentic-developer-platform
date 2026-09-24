from pathlib import Path

from sqlalchemy import create_engine, inspect

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.shared.models.vault import UserCredential
from tests.migrations.conftest_postgres import downgrade, upgrade

MIGRATION = Path(__file__).parents[2] / "alembic/versions/067_aws_connection_verification.py"


def test_aws_verification_migration_is_the_single_head():
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))

    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["069_aws_verification_binding"]
    revision = scripts.get_revision("067_aws_connection_verify")
    assert revision.down_revision == "066_vault_operation_fingerprint"


def test_model_exposes_nullable_server_owned_verification_timestamp():
    column = UserCredential.__table__.c.aws_verified_at

    assert column.nullable is True
    assert column.type.timezone is True


def test_upgrade_adds_nullable_verification_timestamp(pg_url):
    upgrade(pg_url, "066_vault_operation_fingerprint")
    engine = create_engine(pg_url)
    with engine.connect() as connection:
        assert "aws_verified_at" not in {column["name"] for column in inspect(connection).get_columns("user_credentials")}

    upgrade(pg_url, "067_aws_connection_verify")
    with engine.connect() as connection:
        column = next(column for column in inspect(connection).get_columns("user_credentials") if column["name"] == "aws_verified_at")
        assert column["nullable"] is True
        assert column["type"].timezone is True

    downgrade(pg_url, "066_vault_operation_fingerprint")
    with engine.connect() as connection:
        assert "aws_verified_at" not in {column["name"] for column in inspect(connection).get_columns("user_credentials")}
    engine.dispose()
