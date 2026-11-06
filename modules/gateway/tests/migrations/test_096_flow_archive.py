"""Archive state extends the complete migration graph without losing retained flows."""

import ast
import csv
from datetime import UTC, datetime

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, run_alembic, upgrade


def test_archive_migration_preserves_every_inventoried_parent():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert "096_flow_archive" in {revision.revision for revision in scripts.walk_revisions()}
    assert scripts.get_revision("096_flow_archive").down_revision == "095_crew_restore_pins"
    ancestors = {revision.revision for revision in scripts.walk_revisions(base="base", head="096_flow_archive")}
    inventory = GATEWAY_ROOT.parents[1] / "docs/design/flow-self-recovery-7572/gateway-migrations.tsv"
    with inventory.open() as source:
        for row in csv.DictReader(source, delimiter="\t"):
            assert row["revision"] in ancestors
            assert scripts.get_revision(row["revision"]).down_revision == ast.literal_eval(row["down_revision"])


def test_archive_upgrade_preserves_flows_and_downgrade_refuses_hidden_work(pg_url):
    upgrade(pg_url, "095_crew_restore_pins")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO orchestration_flows (id, org_id, slug, title, state, execution_paused) "
                    "VALUES ('retained', 'tenant', 'reserved-slug', 'Retained flow', 'running', false)"
                )
            )
        upgrade(pg_url, "096_flow_archive")
        with engine.begin() as connection:
            columns = {column["name"]: column for column in inspect(connection).get_columns("orchestration_flows")}
            assert columns["archived_at"]["nullable"] is True
            assert columns["archived_at"]["type"].timezone is True
            indexes = {index["name"]: index for index in inspect(connection).get_indexes("orchestration_flows")}
            assert indexes["ix_orchestration_flows_org_archived_created"]["column_names"] == ["org_id", "archived_at", "created_at"]
            retained = connection.execute(
                text("SELECT slug, state, execution_paused, archived_at FROM orchestration_flows WHERE id='retained'")
            ).one()
            assert retained == (
                "reserved-slug",
                "running",
                False,
                None,
            )
            connection.execute(
                text("UPDATE orchestration_flows SET archived_at=:archived_at WHERE id='retained'"),
                {"archived_at": datetime(2026, 10, 13, tzinfo=UTC)},
            )
        result = run_alembic(pg_url, "downgrade", "095_crew_restore_pins")
        assert result.returncode != 0
        assert "Unarchive or reconcile archived flows" in result.stderr
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT archived_at FROM orchestration_flows WHERE id='retained'")) == datetime(2026, 10, 13, tzinfo=UTC)
            connection.execute(text("UPDATE orchestration_flows SET archived_at=NULL WHERE id='retained'"))
        downgrade(pg_url, "095_crew_restore_pins")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "095_crew_restore_pins"
            assert "archived_at" not in {column["name"] for column in inspect(connection).get_columns("orchestration_flows")}
            assert connection.execute(text("SELECT slug, state, execution_paused FROM orchestration_flows WHERE id='retained'")).one() == (
                "reserved-slug",
                "running",
                False,
            )
        upgrade(pg_url, "head")
        with engine.connect() as connection:
            retained = connection.execute(
                text("SELECT slug, state, execution_paused, archived_at FROM orchestration_flows WHERE id='retained'")
            ).one()
            assert retained == ("reserved-slug", "running", False, None)
    finally:
        engine.dispose()
