"""Gateway deployment installs billing checkpoints without losing native evidence."""

from datetime import UTC, datetime

import pytest
from l5_services.models import Configuration, ConfigurationVersion, CostCollection, Run, WorkerAttempt
from l5_services.models.base import Base as L5Base
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL
from tests.migrations.l5_schema import upgrade_l5_gateway_schema


def test_cost_collection_bridge_extends_gateway_history():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("158_l5_cost_collections").down_revision == "157_merge_transform_inquiry"
    assert all(len(revision.revision) <= 32 for revision in scripts.walk_revisions())


def test_gateway_sqlite_bridge_installs_complete_l5_schema():
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            upgrade_l5_gateway_schema(connection)
            inspector = inspect(connection)
            assert set(L5Base.metadata.tables) <= set(inspector.get_table_names())
            for table in L5Base.metadata.sorted_tables:
                assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.columns.keys())
            assert connection.execute(select(CostCollection)).all() == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_gateway_upgrade_preserves_native_evidence_and_retains_collections(request, backend):
    url = request.getfixturevalue("pg_url") if backend == "postgresql" else "sqlite:///:memory:"
    if backend == "postgresql":
        upgrade(url, "157_merge_transform_inquiry")
    engine = create_engine(url)
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    bridge = scripts.get_revision("158_l5_cost_collections").module
    started_at = datetime(2026, 10, 28, tzinfo=UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    try:
        with engine.begin() as connection:
            if backend == "sqlite":
                connection.execute(text("PRAGMA foreign_keys=ON"))
                upgrade_l5_gateway_schema(connection, through_revision="156_l5_finops_inquiry")
            assert not inspect(connection).has_table(CostCollection.__tablename__)
            attempts = Table(WorkerAttempt.__tablename__, MetaData(), autoload_with=connection)
            connection.execute(
                Configuration.__table__.insert().values(
                    **scope,
                    configuration_id="config-example",
                    service_id="finops",
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
                    service_id="finops",
                    configuration_id="config-example",
                    configuration_revision=1,
                    binding_id="binding-example",
                    trigger_id="manual",
                    binding_generation=1,
                    input={"question": "What was spent?"},
                    admission={"limits": {"max_ce_requests": 4}},
                    deadline_at=started_at,
                    native_job_ref={"conversation_id": "conversation", "turn_id": "turn"},
                )
            )
            connection.execute(
                attempts.insert().values(
                    tenant_id=scope["tenant_id"],
                    run_id="run-example",
                    attempt_id="attempt-example",
                    operation="submit",
                    first_work_id="work-example",
                    first_fence=1,
                    idempotency_token="b" * 64,
                    input_hash="c" * 64,
                    question_hash="d" * 64,
                    started_at=started_at,
                    status="unknown",
                    reconcile_checks=1,
                    native_job_ref={"conversation_id": "conversation", "turn_id": "turn"},
                )
            )
            run_before = dict(connection.execute(select(Run)).mappings().one())
            attempt_before = dict(connection.execute(select(attempts)).mappings().one())

        if backend == "postgresql":
            upgrade(url)
        else:
            with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
                bridge.upgrade()

        with engine.begin() as connection:
            inspector = inspect(connection)
            columns = {column["name"]: column for column in inspector.get_columns(CostCollection.__tablename__)}
            assert set(columns) == set(CostCollection.__table__.columns.keys())
            assert set(inspector.get_pk_constraint(CostCollection.__tablename__)["constrained_columns"]) == {
                "tenant_id",
                "run_id",
                "collection_id",
            }
            (foreign_key,) = inspector.get_foreign_keys(CostCollection.__tablename__)
            assert foreign_key["constrained_columns"] == ["tenant_id", "run_id"]
            assert foreign_key["referred_table"] == Run.__tablename__
            assert foreign_key["referred_columns"] == ["tenant_id", "run_id"]
            assert "ix_l5_cost_collection_run" in {index["name"] for index in inspector.get_indexes(CostCollection.__tablename__)}
            if backend == "postgresql":
                assert isinstance(columns["checkpoint_ref"]["type"], JSONB)
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
            assert dict(connection.execute(select(attempts)).mappings().one()) == attempt_before
            connection.execute(
                CostCollection.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    run_id="run-example",
                    collection_id="collection-example",
                    query_hash="e" * 64,
                    scope_digest="f" * 64,
                    started_at=started_at,
                    status="collecting",
                    request_count=1,
                    page_count=1,
                    coverage="partial",
                    checkpoint_ref={"artifact_id": "page-example", "version_id": "version-example", "sha256": "a" * 64},
                )
            )
            collection_before = dict(connection.execute(select(CostCollection)).mappings().one())

        if backend == "postgresql":
            upgrade(url)
            refused = run_alembic(url, "downgrade", "157_merge_transform_inquiry")
            assert refused.returncode != 0
            assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        else:
            with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="retains work history"):
                    bridge.downgrade()
        with engine.connect() as connection:
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
            assert dict(connection.execute(select(attempts)).mappings().one()) == attempt_before
            assert dict(connection.execute(select(CostCollection)).mappings().one()) == collection_before
    finally:
        engine.dispose()
