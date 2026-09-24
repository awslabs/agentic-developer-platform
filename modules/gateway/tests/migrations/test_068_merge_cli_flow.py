"""Both previously selectable migration heads upgrade to the combined schema."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade


def test_merge_retains_both_existing_migration_histories():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    # One head, with the merge in its ancestry — the point of this test is that
    # the two 067 branches stay merged, not what the current head is called
    # (pinning the name made #5664's 070 fail it).
    heads = scripts.get_heads()
    assert len(heads) == 1, f"expected a single migration head, found {heads}"
    assert "068_merge_cli_flow" in {revision.revision for revision in scripts.walk_revisions("base", heads[0])}
    assert set(scripts.get_revision("068_merge_cli_flow").down_revision) == {
        "067_aws_connection_verify",
        "067_flow_execution_pause",
    }
    assert scripts.get_revision("067_flow_execution_pause").down_revision == "066_cred_evidence_delegation"
    assert scripts.get_revision("067_aws_connection_verify").down_revision == "066_vault_operation_fingerprint"


@pytest.mark.parametrize("initial_head", ["067_aws_connection_verify", "067_flow_execution_pause", "068_merge_cli_flow"])
def test_upgrade_from_either_parent_preserves_controls(pg_url, initial_head):
    upgrade(pg_url, initial_head)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO budget_enforcement_settings VALUES ('global', false, 1, 'operator', NOW())"))
        upgrade(pg_url, "head")
        with engine.connect() as connection:
            schema = inspect(connection)
            credential_columns = {column["name"] for column in schema.get_columns("user_credentials")}
            assert {
                "operation_fingerprint",
                "aws_verified_at",
                "aws_verification_attempt",
                "aws_verified_version_id",
                "aws_verified_binding",
            } <= credential_columns
            assert "execution_paused" in {column["name"] for column in schema.get_columns("orchestration_flows")}
            assert connection.scalar(text("SELECT enabled FROM budget_enforcement_settings WHERE scope_key='global'")) is False
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == ["069_aws_verification_binding"]
    finally:
        engine.dispose()
