"""Existing verification timestamps cannot manufacture version-bound evidence."""

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, upgrade


def test_verification_binding_extends_both_preserved_histories():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    head = scripts.get_current_head()  # raises if histories have diverged again
    assert head is not None
    assert "069_aws_verification_binding" in {revision.revision for revision in scripts.iterate_revisions(head, "base")}
    assert scripts.get_revision("069_aws_verification_binding").down_revision == "068_merge_cli_flow"


def test_upgrade_requires_existing_credentials_to_reverify(pg_url):
    upgrade(pg_url, "068_merge_cli_flow")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user_credentials "
                    "(id, org_id, service, credential_type, label, secret_arn, aws_verified_at, created_at) "
                    "VALUES ('legacy-aws', 'org-test', 'aws', 'aws_role', 'legacy', "
                    "'arn:aws:secretsmanager:us-east-1:123456789012:secret:test', NOW(), NOW())"
                )
            )
        upgrade(pg_url, "head")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT * FROM user_credentials WHERE id='legacy-aws'")).mappings().one()
            assert row["aws_verified_at"] is not None
            columns = {column["name"]: column for column in inspect(connection).get_columns("user_credentials")}
            for name, length in (
                ("aws_verification_attempt", 36),
                ("aws_verified_version_id", 64),
                ("aws_verified_binding", 64),
            ):
                assert row[name] is None
                assert columns[name]["nullable"] is True
                assert columns[name]["type"].length == length
        downgrade(pg_url, "068_merge_cli_flow")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT label FROM user_credentials WHERE id='legacy-aws'")) == "legacy"
            assert "aws_verified_binding" not in {column["name"] for column in inspect(connection).get_columns("user_credentials")}
    finally:
        engine.dispose()
