"""Migration 123 — Dev Box instances record the release their generation was provisioned from (#8223 cause 34)."""

import importlib.util
from pathlib import Path

import pytest

from tests.migrations.conftest_postgres import run_alembic

MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "123_dev_box_release_pins.py"
COLUMNS = ("profile_revision", "source_revision", "crew_bundle_sha256", "launch_template_version")


@pytest.fixture(scope="module")
def module():
    spec = importlib.util.spec_from_file_location("migration_123", MIGRATION)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def test_chains_onto_122_and_fits_the_alembic_version_column(module):
    """The first cut of this migration carried a 33-character id and every ``alembic upgrade
    head`` on Postgres failed with ``value too long for type character varying(32)``."""
    assert module.revision == "123_dev_box_release_pins"
    assert module.down_revision == "122_product_handoff_callbacks"
    assert len(module.revision) <= 32 and len(module.down_revision) <= 32


def test_upgrade_adds_the_four_nullable_pin_columns_and_downgrade_removes_them():
    up = run_alembic("postgresql://example:example@localhost/example", "upgrade", "122_product_handoff_callbacks:123_dev_box_release_pins", "--sql")
    assert up.returncode == 0, up.stderr
    for column in COLUMNS:
        assert f"ALTER TABLE dev_box_instances ADD COLUMN {column}" in up.stdout, column
    assert "NOT NULL" not in up.stdout.split("INSERT INTO alembic_version")[0].split("ALTER TABLE dev_box_instances")[1], (
        "pins stay nullable: rows from before the migration are filled from the host receipt"
    )
    down = run_alembic(
        "postgresql://example:example@localhost/example", "downgrade", "123_dev_box_release_pins:122_product_handoff_callbacks", "--sql"
    )
    assert down.returncode == 0, down.stderr
    for column in COLUMNS:
        assert f"ALTER TABLE dev_box_instances DROP COLUMN {column}" in down.stdout, column
