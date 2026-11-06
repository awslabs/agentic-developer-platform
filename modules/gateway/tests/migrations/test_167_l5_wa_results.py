"""Gateway upgrades retain WA attribution and install task-scoped usage receipts."""

from datetime import UTC, datetime

import pytest
from l5_services.models import Configuration, ConfigurationVersion, Publication, Run
from l5_services.models.evidence import AssessmentResult
from l5_services.models.generation_collection import GenerationCollection
from l5_services.models.wa_usage import WellArchitectedUsage
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL
from tests.migrations.l5_schema import upgrade_l5_gateway_schema


def test_results_bridge_extends_generation_history():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("167_l5_wa_results").down_revision == "166_l5_wa_generation"
    assert all(len(revision.revision) <= 32 for revision in scripts.walk_revisions())


@pytest.mark.parametrize("backend", ["sqlite", "postgresql"])
def test_gateway_upgrade_preserves_collection_and_publication_history(request, backend):
    url = request.getfixturevalue("pg_url") if backend == "postgresql" else "sqlite:///:memory:"
    if backend == "postgresql":
        upgrade(url, "166_l5_wa_generation")
    engine = create_engine(url)
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    bridge = scripts.get_revision("167_l5_wa_results").module
    now = datetime(2026, 10, 28, tzinfo=UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    try:
        with engine.begin() as connection:
            if backend == "sqlite":
                connection.execute(text("PRAGMA foreign_keys=ON"))
                upgrade_l5_gateway_schema(connection, through_revision="166_l5_wa_generation")
            old_collections = Table(GenerationCollection.__tablename__, MetaData(), autoload_with=connection)
            assert "excluded" not in old_collections.c
            assert not inspect(connection).has_table(WellArchitectedUsage.__tablename__)
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
                    deadline_at=now,
                )
            )
            connection.execute(
                old_collections.insert().values(
                    tenant_id=scope["tenant_id"],
                    run_id="run-example",
                    native_job_ref={"generation_id": "generation-example"},
                    next_token="page-two",
                    page_count=1,
                    recommendations=[{"recommendationArn": "retained-recommendation"}],
                    pending_recommendations=[{"recommendationArn": "pending-recommendation"}],
                )
            )
            connection.execute(
                AssessmentResult.__table__.insert().values(
                    tenant_id=scope["tenant_id"], run_id="run-example", result_id="result-example", version=2, payload={"review_mode": "source_only"}
                )
            )
            connection.execute(
                Publication.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    publication_id="publication-example",
                    result_id="result-example",
                    result_version=2,
                    destination="product-context",
                    schema_version="1.0",
                    contribution_hash="b" * 64,
                    payload={"kind": "well_architected_result", "scope": scope},
                    state="published",
                    receipt={"receipt_id": "admission-example"},
                    next_due_at=now,
                )
            )
            collection_before = dict(connection.execute(select(old_collections)).mappings().one())
            result_before = dict(connection.execute(select(AssessmentResult)).mappings().one())
            publication_before = dict(connection.execute(select(Publication)).mappings().one())

        if backend == "postgresql":
            upgrade(url)
            upgrade(url)
        else:
            with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
                bridge.upgrade()

        with engine.begin() as connection:
            inspector = inspect(connection)
            for model in (GenerationCollection, WellArchitectedUsage):
                columns = {column["name"]: column for column in inspector.get_columns(model.__tablename__)}
                assert set(columns) == set(model.__table__.columns.keys())
                assert set(inspector.get_pk_constraint(model.__tablename__)["constrained_columns"]) == {
                    column.name for column in model.__table__.primary_key
                }
            (foreign_key,) = inspector.get_foreign_keys(WellArchitectedUsage.__tablename__)
            assert foreign_key["constrained_columns"] == ["tenant_id", "publication_id"]
            assert foreign_key["referred_table"] == Publication.__tablename__
            assert foreign_key["referred_columns"] == ["tenant_id", "publication_id"]
            for field in ("retrieval_receipt", "usage_receipt"):
                assert not columns[field]["nullable"]
                if backend == "postgresql":
                    assert isinstance(columns[field]["type"], JSONB)
            excluded_column = next(column for column in inspector.get_columns(GenerationCollection.__tablename__) if column["name"] == "excluded")
            assert not excluded_column["nullable"]
            if backend == "postgresql":
                assert isinstance(excluded_column["type"], JSONB)
            assert dict(connection.execute(select(GenerationCollection)).mappings().one()) == {**collection_before, "excluded": []}
            assert dict(connection.execute(select(AssessmentResult)).mappings().one()) == result_before
            assert dict(connection.execute(select(Publication)).mappings().one()) == publication_before
            excluded = [{"recommendationArn": "other-recommendation", "generationId": "other-generation", "reason": "different_generation"}]
            connection.execute(update(GenerationCollection).values(excluded=excluded))
            usage = {
                "tenant_id": scope["tenant_id"],
                "publication_id": "publication-example",
                "task_id": "later-task",
                "result_id": "result-example",
                "actor_id": "actor-example",
                "retrieval_receipt": {"receipt_id": "retrieval-example"},
                "usage_receipt": {"receipt_id": "usage-example", "retrieval_receipt_id": "retrieval-example"},
                "observed_at": now,
            }
            connection.execute(WellArchitectedUsage.__table__.insert().values(**usage))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(WellArchitectedUsage.__table__.insert().values(**usage))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(WellArchitectedUsage.__table__.insert().values({**usage, "tenant_id": "foreign-tenant"}))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    WellArchitectedUsage.__table__.insert().values({**usage, "task_id": "missing-publication-task", "publication_id": "missing"})
                )
            connection.execute(WellArchitectedUsage.__table__.insert().values({**usage, "task_id": "another-task"}))
            usages_before = list(connection.execute(select(WellArchitectedUsage).order_by(WellArchitectedUsage.task_id)).mappings())
            assert len(usages_before) == 2
            assert all(
                row["retrieval_receipt"] == usage["retrieval_receipt"] and row["usage_receipt"] == usage["usage_receipt"] for row in usages_before
            )

        if backend == "postgresql":
            upgrade(url)
            refused = run_alembic(url, "downgrade", "166_l5_wa_generation")
            assert refused.returncode != 0
            assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        else:
            with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(RuntimeError, match="retains work history"):
                    bridge.downgrade()
        with engine.connect() as connection:
            if backend == "postgresql":
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(GenerationCollection)).mappings().one()) == {**collection_before, "excluded": excluded}
            assert dict(connection.execute(select(AssessmentResult)).mappings().one()) == result_before
            assert dict(connection.execute(select(Publication)).mappings().one()) == publication_before
            assert list(connection.execute(select(WellArchitectedUsage).order_by(WellArchitectedUsage.task_id)).mappings()) == usages_before
    finally:
        engine.dispose()
