"""Admin revocations and Product receipts survive convergence of both migration branches."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_admin_and_source_branches_have_one_merged_head():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert set(scripts.get_revision("139_merge_admin_sources").down_revision) == {
        "137_platform_admin_grants",
        "138_product_source_snapshots",
    }
    assert all(len(revision.revision) <= 32 for revision in scripts.iterate_revisions("head", "136_tool_adapter_activation"))


@pytest.mark.parametrize("previous_head", ["137_platform_admin_grants", "138_product_source_snapshots"])
def test_upgrade_from_either_branch_and_refused_rollback_preserve_security_state(pg_url, previous_head):
    admin_seed = text("INSERT INTO platform_admin_grants (cognito_sub, enabled, updated_at) VALUES ('revoked-login', false, '2026-10-01T00:00:00Z')")
    receipt_seed = text("""INSERT INTO product_source_snapshots
        (tenant_id, id, product_id, project_id, delegation_id, delegation_revision,
         source_ref, receipt, idempotency_key, request_hash, retain_until, expires_at, created_at)
        VALUES ('tenant-a', 'snapshot-a', 'product-a', 'project-a', 'delegation-a', 1,
                '{}'::jsonb, '{"source_revision":"original"}'::jsonb, 'once', 'request-digest',
                '2026-12-01T00:00:00Z', '2026-12-01T00:00:00Z', '2026-10-01T00:00:00Z')""")
    admin_read = text("SELECT enabled FROM platform_admin_grants WHERE cognito_sub = 'revoked-login'")
    receipt_read = text("SELECT receipt FROM product_source_snapshots WHERE tenant_id = 'tenant-a' AND id = 'snapshot-a'")
    from_admin = previous_head == "137_platform_admin_grants"
    upgrade(pg_url, previous_head)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(admin_seed if from_admin else receipt_seed)
        upgrade(pg_url, "139_merge_admin_sources")
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "139_merge_admin_sources"
            assert {"platform_admin_grants", "credential_service_delegations", "product_source_snapshots"} <= set(
                inspect(connection).get_table_names()
            )
            if from_admin:
                assert connection.scalar(admin_read) is False
                connection.execute(receipt_seed)
            else:
                assert connection.scalar(receipt_read) == {"source_revision": "original"}
                connection.execute(admin_seed)
        refused = run_alembic(pg_url, "downgrade", "136_tool_adapter_activation")
        assert refused.returncode != 0
        assert "Retain Product source receipts through rollback and admission drain" in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "139_merge_admin_sources"
            assert connection.scalar(admin_read) is False
            assert connection.scalar(receipt_read) == {"source_revision": "original"}
    finally:
        engine.dispose()
