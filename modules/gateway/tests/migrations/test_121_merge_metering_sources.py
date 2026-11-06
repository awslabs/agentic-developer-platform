"""Both existing migration heads can converge without replaying their own schema."""

import pytest

from tests.migrations.conftest_postgres import run_alembic


@pytest.mark.parametrize(
    "starting_revision, incoming_table, retained_table",
    [
        ("120_merge_metering_initiatives", "product_source_bindings", "metering_attempts"),
        ("118_product_verification_ops", "metering_attempts", "product_source_bindings"),
    ],
)
def test_offline_upgrade_from_either_head_preserves_existing_schema(starting_revision, incoming_table, retained_table):
    result = run_alembic("postgresql://example:example@localhost/example", "upgrade", f"{starting_revision}:121_merge_metering_sources", "--sql")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count(f"CREATE TABLE {incoming_table} (") == 1
    assert f"CREATE TABLE {retained_table} (" not in result.stdout
    assert "CREATE TABLE catalog_initiative_versions (" not in result.stdout
    assert "DROP TABLE" not in result.stdout
    assert "DROP COLUMN" not in result.stdout
    assert "121_merge_metering_sources" in result.stdout
    if incoming_table == "product_source_bindings":
        assert "ALTER TABLE product_operations ADD COLUMN lease_owner" in result.stdout
    else:
        assert "ALTER TABLE product_operations" not in result.stdout
