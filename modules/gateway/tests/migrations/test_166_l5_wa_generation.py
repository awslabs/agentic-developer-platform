"""Gateway upgrades retain native attempts and install resumable WA collection."""

from datetime import UTC, datetime

import pytest
from l5_services.models import Configuration, ConfigurationVersion, Run, WorkerAttempt
from l5_services.models.generation_collection import GenerationCollection
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_generation_bridge_extends_retention_history():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("166_l5_wa_generation").down_revision == "165_l5_source_retention"
    assert all(len(revision.revision) <= 32 for revision in scripts.walk_revisions())


def test_gateway_upgrade_preserves_attempts_and_collection_checkpoints(pg_url):
    upgrade(pg_url, "165_l5_source_retention")
    engine = create_engine(pg_url)
    now = datetime(2026, 10, 28, tzinfo=UTC)
    try:
        with engine.begin() as connection:
            old_attempts = Table(WorkerAttempt.__tablename__, MetaData(), autoload_with=connection)
            assert "prepared_request" not in old_attempts.c
            assert not inspect(connection).has_table(GenerationCollection.__tablename__)
            connection.execute(
                Configuration.__table__.insert().values(
                    tenant_id="tenant-example",
                    configuration_id="config-example",
                    product_id="product-example",
                    project_id="project-example",
                    service_id="well_architected",
                    relationship_revision="1",
                    owner_id="owner-example",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection.execute(
                ConfigurationVersion.__table__.insert().values(
                    tenant_id="tenant-example",
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
                    tenant_id="tenant-example",
                    run_id="run-example",
                    product_id="product-example",
                    project_id="project-example",
                    service_id="well_architected",
                    configuration_id="config-example",
                    configuration_revision=1,
                    binding_id="binding-example",
                    trigger_id="manual",
                    binding_generation=1,
                    input={"capability": "architecture_review"},
                    deadline_at=now,
                )
            )
            connection.execute(
                old_attempts.insert().values(
                    tenant_id="tenant-example",
                    attempt_id="attempt-example",
                    run_id="run-example",
                    operation="submit",
                    first_work_id="work-example",
                    first_fence=1,
                    idempotency_token="b" * 64,
                    input_hash="c" * 64,
                    question_hash="d" * 64,
                    started_at=now,
                    status="unknown",
                    reconcile_checks=1,
                    native_job_ref={"generation_arn": "generation-example"},
                )
            )
            attempt_before = dict(connection.execute(select(old_attempts)).mappings().one())
            run_before = dict(connection.execute(select(Run)).mappings().one())

        upgrade(pg_url)
        with engine.begin() as connection:
            inspector = inspect(connection)
            for model in (WorkerAttempt, GenerationCollection):
                columns = {column["name"]: column for column in inspector.get_columns(model.__tablename__)}
                assert set(columns) == set(model.__table__.columns.keys())
            attempt_columns = {column["name"]: column for column in inspector.get_columns(WorkerAttempt.__tablename__)}
            assert isinstance(attempt_columns["prepared_request"]["type"], JSONB)
            assert attempt_columns["prepared_request"]["nullable"]
            assert isinstance(columns["pending_recommendations"]["type"], JSONB)
            assert not columns["pending_recommendations"]["nullable"]
            assert dict(connection.execute(select(WorkerAttempt)).mappings().one()) == {**attempt_before, "prepared_request": None}
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
            request = {"generation_name": "generation-example", "profile_arn": "profile-example", "scope": {"project_id": "project-example"}}
            connection.execute(update(WorkerAttempt).values(prepared_request=request))
            checkpoint = {
                "tenant_id": "tenant-example",
                "run_id": "run-example",
                "native_job_ref": {"generation_arn": "generation-example"},
                "next_token": "page-two",
                "page_count": 1,
                "recommendations": [],
            }
            connection.execute(GenerationCollection.__table__.insert().values(**checkpoint))
            assert connection.execute(select(GenerationCollection.pending_recommendations)).scalar_one() == []
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(GenerationCollection.__table__.insert().values(**checkpoint))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(GenerationCollection.__table__.insert().values({**checkpoint, "tenant_id": "foreign-tenant"}))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(update(GenerationCollection).values(page_count=0))
            connection.execute(update(GenerationCollection).values(pending_recommendations=[{"recommendationId": "recommendation-example"}]))
            attempt_saved = dict(connection.execute(select(WorkerAttempt)).mappings().one())
            collection_saved = dict(connection.execute(select(GenerationCollection)).mappings().one())

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "165_l5_source_retention")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(WorkerAttempt)).mappings().one()) == attempt_saved
            assert dict(connection.execute(select(GenerationCollection)).mappings().one()) == collection_saved
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
    finally:
        engine.dispose()
