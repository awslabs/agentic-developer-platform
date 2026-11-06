"""Gateway upgrades install source holds without losing retained run history."""

from datetime import UTC, datetime, timedelta

import pytest
from l5_services.models import Configuration, ConfigurationVersion, Run, SourceRetention
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_source_retention_bridge_extends_gateway_history():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("165_l5_source_retention").down_revision == "164_merge_wa_connector"
    assert scripts.get_revision("164_merge_wa_connector").down_revision == ("159_merge_wa_cost", "163_merge_connector_cost")
    assert all(len(revision.revision) <= 32 for revision in scripts.walk_revisions())


def test_gateway_upgrade_preserves_runs_and_retains_source_holds(pg_url):
    upgrade(pg_url, "164_merge_wa_connector")
    engine = create_engine(pg_url)
    now = datetime(2026, 10, 28, tzinfo=UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    try:
        with engine.begin() as connection:
            assert not inspect(connection).has_table(SourceRetention.__tablename__)
            connection.execute(
                Configuration.__table__.insert().values(
                    **scope,
                    configuration_id="config-example",
                    service_id="well_architected",
                    relationship_revision="1",
                    owner_id="owner-example",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection.execute(
                ConfigurationVersion.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    configuration_id="config-example",
                    revision=1,
                    settings={},
                    inherited_fields=[],
                    gaps=[],
                    state="ready",
                    hash="a" * 64,
                )
            )
            connection.execute(
                Run.__table__.insert().values(
                    **scope,
                    run_id="run-example",
                    service_id="well_architected",
                    configuration_id="config-example",
                    configuration_revision=1,
                    binding_id="binding-example",
                    trigger_id="manual",
                    binding_generation=1,
                    input={"capability": "architecture_review"},
                    deadline_at=now + timedelta(hours=1),
                    native_job_ref={"generation_id": "native-example"},
                    provider_state="running",
                )
            )
            run_before = dict(connection.execute(select(Run)).mappings().one())

        upgrade(pg_url)
        with engine.begin() as connection:
            inspector = inspect(connection)
            columns = {column["name"]: column for column in inspector.get_columns(SourceRetention.__tablename__)}
            assert set(columns) == set(SourceRetention.__table__.columns.keys())
            assert isinstance(columns["artifact_ref"]["type"], JSONB)
            assert isinstance(columns["last_evidence"]["type"], JSONB)
            assert set(inspector.get_pk_constraint(SourceRetention.__tablename__)["constrained_columns"]) == {"tenant_id", "artifact_id", "run_id"}
            (foreign_key,) = inspector.get_foreign_keys(SourceRetention.__tablename__)
            assert foreign_key["constrained_columns"] == ["tenant_id", "run_id"]
            assert foreign_key["referred_table"] == Run.__tablename__
            assert foreign_key["referred_columns"] == ["tenant_id", "run_id"]
            assert "ix_l5_source_retention_artifact" in {index["name"] for index in inspector.get_indexes(SourceRetention.__tablename__)}
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
            hold = {
                "tenant_id": scope["tenant_id"],
                "project_id": scope["project_id"],
                "artifact_id": "source-example",
                "run_id": "run-example",
                "artifact_ref": {"artifact_id": "source-example", "version": "v1", "sha256": "a" * 64},
                "required_until": now + timedelta(days=2),
                "policy_expires_at": now + timedelta(days=4),
                "reason": "remote_generation",
            }
            connection.execute(SourceRetention.__table__.insert().values(**hold))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(SourceRetention.__table__.insert().values(**hold))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(SourceRetention.__table__.insert().values({**hold, "tenant_id": "another-tenant"}))
            hold_before = dict(connection.execute(select(SourceRetention)).mappings().one())
            assert hold_before["released_at"] is None

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "164_merge_wa_connector")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
            assert dict(connection.execute(select(SourceRetention)).mappings().one()) == hold_before
    finally:
        engine.dispose()
