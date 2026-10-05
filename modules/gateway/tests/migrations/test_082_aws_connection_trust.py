"""Upgrade never fabricates trust provenance for an existing credential."""

from sqlalchemy import create_engine, inspect, text

from tests.migrations.conftest_postgres import downgrade, upgrade


def test_legacy_connection_has_no_server_issued_trust_after_upgrade(pg_url):
    upgrade(pg_url, "081_persona_platform_defaults")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO user_credentials (id, org_id, service, credential_type, label, secret_arn, created_at) "
                    "VALUES ('legacy-aws', 'org-test', 'aws', 'aws_role', 'legacy', 'synthetic-secret', NOW())"
                )
            )
        upgrade(pg_url, "082_aws_connection_trust")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT aws_external_id FROM user_credentials WHERE id='legacy-aws'")).one() == (None,)
            assert connection.scalar(text("SELECT scopes->>'status' FROM user_credentials WHERE id='legacy-aws'")) == "pending"
            assert connection.scalar(text("SELECT aws_verified_at FROM user_credentials WHERE id='legacy-aws'")) is None
        downgrade(pg_url, "081_persona_platform_defaults")
        with engine.connect() as connection:
            assert "aws_external_id" not in {c["name"] for c in inspect(connection).get_columns("user_credentials")}
            assert connection.scalar(text("SELECT label FROM user_credentials WHERE id='legacy-aws'")) == "legacy"
    finally:
        engine.dispose()
