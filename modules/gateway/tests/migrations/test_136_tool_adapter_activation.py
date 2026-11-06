"""Adding the release fence never activates an adapter on migration."""

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.migrations.conftest_postgres import upgrade


@pytest.mark.parametrize(
    "target_revision",
    [
        "136_tool_adapter_activation",
        "137_platform_admin_grants",
        "138_product_source_snapshots",
        "139_merge_admin_sources",
        "138_tool_adapter_transition",
        "140_merge_adapter_sources",
    ],
)
def test_adapter_activation_migration_is_additive_and_fenced_by_default(pg_url, target_revision):
    upgrade(pg_url, "135_mcp_identity_completion")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO oauth_connection_attempts
                (state_digest, org_id, user_id, provider, session_uri, expires_at)
                VALUES ('prior-state', 'org-acme', 'user-alice', 'atlassian-mcp', 'prior-session', now())""")
            )
    finally:
        engine.dispose()
    upgrade(pg_url, target_revision)
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == target_revision
            assert inspect(connection).has_table("tool_adapter_activations")
            assert connection.scalar(text("SELECT count(*) FROM tool_adapter_activations")) == 0
            columns = {column["name"] for column in inspect(connection).get_columns("tool_adapter_activations")}
            expected_columns = {"group", "status", "release"}
            if target_revision in {"138_tool_adapter_transition", "140_merge_adapter_sources"}:
                expected_columns.add("transition")
            assert columns == expected_columns
            receipt_columns = {column["name"] for column in inspect(connection).get_columns("oauth_tool_receipts")}
            assert {"authorization", "consumed_at"} <= receipt_columns
            assert connection.scalar(text("SELECT count(*) FROM oauth_tool_receipts")) == 0
            assert inspect(connection).has_table("oauth_grant_revocations")
            grant_columns = {column["name"] for column in inspect(connection).get_columns("oauth_grant_revocations")}
            assert {"tenant_id", "owner_ref", "grant_revision", "source_kind", "state", "lease_expires_at"} <= grant_columns
            assert connection.scalar(text("SELECT count(*) FROM oauth_grant_revocations")) == 0
            attempts_columns = {column["name"] for column in inspect(connection).get_columns("oauth_connection_attempts")}
            assert {"identity_completion_started_at", "superseded_at"} <= attempts_columns
            assert (
                connection.scalar(text("SELECT count(*) FROM oauth_connection_attempts WHERE state_digest = 'prior-state' AND superseded_at IS NULL"))
                == 1
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "target_revision,barrier",
    [
        ("136_tool_adapter_activation", "Retain adapter activation and group generation during rollback"),
        ("137_platform_admin_grants", "Retain adapter activation and group generation during rollback"),
        ("138_product_source_snapshots", "Retain Product source receipts through rollback and admission drain"),
        ("139_merge_admin_sources", "Retain Product source receipts through rollback and admission drain"),
        ("138_tool_adapter_transition", "Retain the adapter transition and its rollback manifest"),
        ("140_merge_adapter_sources", "Retain the adapter transition and its rollback manifest"),
    ],
)
def test_adapter_activation_downgrade_preserves_release_generation(pg_url, target_revision, barrier):
    from tests.migrations.conftest_postgres import run_alembic

    upgrade(pg_url, target_revision)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO tool_adapter_activations (\"group\", status, release) VALUES ('atlassian', 'fenced', CAST(:release AS json))"),
                {"release": '{"group":"atlassian","group_generation":2}'},
            )
            connection.execute(
                text("""INSERT INTO oauth_grant_revocations
                (id, tenant_id, owner_ref, grant_revision, source_kind, state, due_at, attempts)
                VALUES ('pending-grant', 'org-acme', 'user-alice', 'revision-1', 'binding', 'pending', now(), 0)""")
            )
        refused = run_alembic(pg_url, "downgrade", "135_mcp_identity_completion")
        assert refused.returncode != 0
        assert barrier in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == target_revision
            assert connection.scalar(text("SELECT release->>'group_generation' FROM tool_adapter_activations")) == "2"
            assert connection.scalar(text("SELECT state FROM oauth_grant_revocations WHERE id = 'pending-grant'")) == "pending"
    finally:
        engine.dispose()
