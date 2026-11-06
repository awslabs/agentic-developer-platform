"""Both independently deployed revision-128 branches reach the same protected head."""

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


@pytest.mark.parametrize("previous_head", ["128_native_epic_issue_links", "128_atlassian_setup_recovery"])
def test_both_upgrade_paths_preserve_native_links_and_atlassian_recovery(pg_url, previous_head):
    upgrade(pg_url, previous_head)
    upgrade(pg_url, "head")
    engine = sa.create_engine(pg_url)
    try:
        with engine.begin() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert {"parent_repository_id", "parent_issue_id", "link_confirmed_at"} <= {
                column["name"] for column in sa.inspect(connection).get_columns("orchestration_amendment_issue_bindings")
            }
            assert sa.inspect(connection).has_table("oauth_recovery_jobs")
            assert "publication_pending_digest" in {
                column["name"] for column in sa.inspect(connection).get_columns("orchestration_environment_leases")
            }
            connection.execute(
                sa.text(
                    "INSERT INTO oauth_setup_states (deployment_key, tenant_id, registration_mode, config_digest, phase, updated_at) "
                    "VALUES ('setup', 'tenant', 'retained-deployment-client', :digest, 'pending', now())"
                ),
                {"digest": "a" * 64},
            )
        rejected = run_alembic(pg_url, "downgrade", "127_authoring_assignment")
        assert rejected.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in rejected.stderr
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT count(*) FROM oauth_setup_states WHERE deployment_key='setup'")) == 1
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
    finally:
        engine.dispose()


def test_both_branch_parents_converge_to_one_head():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert set(scripts.get_revision("129_merge_native_epic_atlassian").down_revision) == {
        "128_native_epic_issue_links",
        "128_atlassian_setup_recovery",
    }
