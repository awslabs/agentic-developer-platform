"""Independent PostgreSQL verification of the identity/pricing merge revision."""

import pytest
from sqlalchemy import create_engine, inspect, text

from tests.migrations.conftest_postgres import downgrade, upgrade


@pytest.mark.parametrize("predecessor", ["base", "070_opus55_pricing", "071_installation_revocation"])
def test_both_existing_heads_and_fresh_database_upgrade_without_losing_revocation_guard(pg_url, predecessor):
    engine = create_engine(pg_url)
    try:
        if predecessor != "base":
            upgrade(pg_url, predecessor)
        if predecessor == "071_installation_revocation":
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO installation_revocations "
                        "(installation_id, org_id, authorized_user_ids, cleanup_pending, revoked_at) "
                        "VALUES ('123456', 'retained-owner', '[\"original-installer\"]', '[\"identity_index_denial\"]', NOW())"
                    )
                )
        upgrade(pg_url)
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "072_merge_identity_pricing"
            assert "delivery_method" in {column["name"] for column in inspect(connection).get_columns("magic_link_nonces")}
            assert connection.scalar(text("SELECT count(*) FROM model_pricing_rates_v2 WHERE model_id = 'anthropic.claude-opus-5-5'")) > 0
            pricing_before = tuple(
                connection.execute(
                    text("SELECT current_generation_id, pointer_revision, consumers_enabled, refresh_paused FROM model_pricing_active")
                ).one()
            )
            if predecessor != "071_installation_revocation":
                connection.execute(
                    text(
                        "INSERT INTO installation_revocations "
                        "(installation_id, org_id, authorized_user_ids, cleanup_pending, revoked_at) "
                        "VALUES ('123456', 'retained-owner', '[\"original-installer\"]', '[\"identity_index_denial\"]', NOW())"
                    )
                )
            record_before = tuple(connection.execute(text("SELECT * FROM installation_revocations")).one())

        # Crossing 071 would discard active denial. The merge cannot bypass its
        # downgrade guard or partially advance the version/pricing pointer.
        with pytest.raises(AssertionError, match="Cannot discard active installation revocations"):
            downgrade(pg_url, "070_magic_link_delivery_method")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "072_merge_identity_pricing"
            assert tuple(connection.execute(text("SELECT * FROM installation_revocations")).one()) == record_before
            assert (
                tuple(
                    connection.execute(
                        text("SELECT current_generation_id, pointer_revision, consumers_enabled, refresh_paused FROM model_pricing_active")
                    ).one()
                )
                == pricing_before
            )

        # Unjoining the no-op merge itself must retain active denial. Returning
        # to the merged head must not duplicate or change pricing publication.
        downgrade(pg_url, "071_installation_revocation")
        with engine.connect() as connection:
            assert tuple(connection.execute(text("SELECT * FROM installation_revocations")).one()) == record_before
        upgrade(pg_url)
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "072_merge_identity_pricing"
            assert (
                tuple(
                    connection.execute(
                        text("SELECT current_generation_id, pointer_revision, consumers_enabled, refresh_paused FROM model_pricing_active")
                    ).one()
                )
                == pricing_before
            )
            connection.execute(text("UPDATE installation_revocations SET restored_at = NOW(), cleanup_pending = '[]'"))
        downgrade(pg_url, "070_magic_link_delivery_method")
        with engine.connect() as connection:
            assert "installation_revocations" not in inspect(connection).get_table_names()
    finally:
        engine.dispose()
