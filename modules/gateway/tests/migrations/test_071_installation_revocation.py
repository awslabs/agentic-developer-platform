"""Real PostgreSQL denial retention and safe downgrade for installation teardown."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, upgrade


def test_revocation_extends_magic_link_delivery_history():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    ancestors = {rev.revision for rev in scripts.walk_revisions("base", heads[0])}
    assert {"071_installation_revocation", "070_opus55_pricing"} <= ancestors
    assert scripts.get_revision("071_installation_revocation").down_revision == "070_magic_link_delivery_method"


def test_denial_survives_owner_removal_and_blocks_downgrade(pg_url):
    upgrade(pg_url, "070_magic_link_delivery_method")
    upgrade(pg_url, "071_installation_revocation")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id, name, aws_accounts, role_mappings, settings, created_at) "
                    "VALUES ('revoked-owner', 'Owner', '[]', '{}', '{}', NOW())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO installation_revocations "
                    "(installation_id, org_id, authorized_user_ids, cleanup_pending, revoked_at) "
                    "VALUES ('123456', 'revoked-owner', '[\"installer\"]', '[\"provider_uninstall\"]', NOW())"
                )
            )
            connection.execute(text("DELETE FROM organizations WHERE id = 'revoked-owner'"))
        with engine.connect() as connection:
            row = connection.execute(text("SELECT * FROM installation_revocations")).mappings().one()
            assert row["authorized_user_ids"] == ["installer"]
            assert row["cleanup_pending"] == ["provider_uninstall"]
            assert row["provider_revoked"] is False
            assert row["restored_at"] is None
            assert inspect(connection).get_foreign_keys("installation_revocations") == []
        with pytest.raises(AssertionError, match="Cannot discard active installation revocations"):
            downgrade(pg_url, "070_magic_link_delivery_method")
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT count(*) FROM installation_revocations WHERE restored_at IS NULL")) == 1
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "071_installation_revocation"
            connection.execute(text("UPDATE installation_revocations SET restored_at = NOW(), cleanup_pending = '[]'"))
        downgrade(pg_url, "070_magic_link_delivery_method")
        with engine.connect() as connection:
            assert "installation_revocations" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()
