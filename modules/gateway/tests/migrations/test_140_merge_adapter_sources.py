"""Converging migration branches retains rollback state and source receipts."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_adapter_and_source_branches_converge_without_rewriting_published_revisions():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert set(scripts.get_revision("140_merge_adapter_sources").down_revision) == {
        "139_merge_admin_sources",
        "138_tool_adapter_transition",
    }
    assert scripts.get_revision("138_tool_adapter_transition").down_revision == "137_platform_admin_grants"
    assert set(scripts.get_revision("139_merge_admin_sources").down_revision) == {
        "137_platform_admin_grants",
        "138_product_source_snapshots",
    }
    assert all(len(revision.revision) <= 32 for revision in scripts.walk_revisions())


@pytest.mark.parametrize("previous_head", ["138_tool_adapter_transition", "139_merge_admin_sources"])
def test_upgrading_either_head_retains_original_recovery_state(pg_url, previous_head):
    adapter_seed = text("""INSERT INTO tool_adapter_activations ("group", status, release, transition)
        VALUES ('tool-connectors', 'fenced', '{"group_generation": 2}'::json,
                '{"phase":"rollback","original_operation_id":"original"}'::json)""")
    receipt_seed = text("""INSERT INTO product_source_snapshots
        (tenant_id, id, product_id, project_id, delegation_id, delegation_revision,
         source_ref, receipt, idempotency_key, request_hash, retain_until, expires_at, created_at)
        VALUES ('tenant-a', 'snapshot-a', 'product-a', 'project-a', 'delegation-a', 1,
                '{}'::jsonb, '{"source_revision":"original"}'::jsonb, 'once', 'request-digest',
                '2026-12-01T00:00:00Z', '2026-12-01T00:00:00Z', '2026-10-01T00:00:00Z')""")
    adapter_read = text("SELECT transition FROM tool_adapter_activations WHERE \"group\" = 'tool-connectors'")
    receipt_read = text("SELECT receipt FROM product_source_snapshots WHERE id = 'snapshot-a'")
    from_adapter = previous_head == "138_tool_adapter_transition"
    upgrade(pg_url, previous_head)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(adapter_seed if from_adapter else receipt_seed)
            connection.execute(text("INSERT INTO platform_admin_grants (cognito_sub, enabled, updated_at) VALUES ('revoked-login', false, now())"))
        upgrade(pg_url, "140_merge_adapter_sources")
        with engine.begin() as connection:
            assert connection.scalar(adapter_read if from_adapter else receipt_read) == (
                {"phase": "rollback", "original_operation_id": "original"} if from_adapter else {"source_revision": "original"}
            )
            connection.execute(receipt_seed if from_adapter else adapter_seed)
            assert {"tool_adapter_activations", "platform_admin_grants", "credential_service_delegations", "product_source_snapshots"} <= set(
                inspect(connection).get_table_names()
            )
        refused = run_alembic(pg_url, "downgrade", "136_tool_adapter_activation")
        assert refused.returncode != 0
        assert "Retain the adapter transition and its rollback manifest" in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "140_merge_adapter_sources"
            assert connection.scalar(adapter_read) == {"phase": "rollback", "original_operation_id": "original"}
            assert connection.scalar(receipt_read) == {"source_revision": "original"}
            assert connection.scalar(text("SELECT enabled FROM platform_admin_grants WHERE cognito_sub = 'revoked-login'")) is False
    finally:
        engine.dispose()
