"""Gateway publication storage preserves results and durable consumer work."""

from datetime import UTC, datetime

import pytest
from l5_services.models import AssessmentResult, Configuration, ConfigurationVersion, Publication, Run
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_publication_bridge_extends_the_existing_gateway_head():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("153_l5_publications").down_revision == "152_merge_gitlab_prevention"
    assert scripts.get_revision("152_merge_gitlab_prevention").down_revision == ("151_merge_gitlab_l5_runs", "151_l5_devops_prevention_obs")


@pytest.mark.parametrize("state", ["retry_wait", "published", "invalidation_pending"])
def test_gateway_upgrade_preserves_results_and_publication_obligations(pg_url, state):
    upgrade(pg_url, "152_merge_gitlab_prevention")
    engine = create_engine(pg_url)
    observed_at = datetime(2026, 10, 28, tzinfo=UTC)
    scope = {"tenant_id": "tenant", "product_id": "product", "project_id": "project"}
    try:
        with engine.begin() as connection:
            assert not inspect(connection).has_table(Publication.__tablename__)
            connection.execute(
                Configuration.__table__.insert().values(
                    **scope,
                    configuration_id="config",
                    service_id="security",
                    relationship_revision="1",
                    owner_id="owner",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection.execute(
                ConfigurationVersion.__table__.insert().values(
                    tenant_id="tenant",
                    configuration_id="config",
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
                    run_id="retained-run",
                    service_id="security",
                    configuration_id="config",
                    configuration_revision=1,
                    binding_id="binding",
                    trigger_id="manual",
                    binding_generation=1,
                    input={"service_id": "security"},
                    admission={"scope": scope},
                    state="succeeded",
                    provider_state="completed",
                    deadline_at=observed_at,
                )
            )
            connection.execute(
                AssessmentResult.__table__.insert().values(
                    tenant_id="tenant",
                    result_id="retained-result",
                    run_id="retained-run",
                    version=1,
                    payload={"native_job_id": "retained-native-job", "findings": []},
                )
            )
            run_before = dict(connection.execute(select(Run)).mappings().one())
            result_before = dict(connection.execute(select(AssessmentResult)).mappings().one())

        upgrade(pg_url)
        with engine.begin() as connection:
            inspector = inspect(connection)
            columns = inspector.get_columns(Publication.__tablename__)
            assert {column["name"] for column in columns} == set(Publication.__table__.columns.keys())
            for name in ("payload", "receipt", "invalidation", "invalidation_receipt"):
                assert isinstance(next(column for column in columns if column["name"] == name)["type"], JSONB)
            (foreign_key,) = inspector.get_foreign_keys(Publication.__tablename__)
            assert foreign_key["constrained_columns"] == ["tenant_id", "result_id"]
            assert foreign_key["referred_table"] == AssessmentResult.__tablename__
            assert foreign_key["referred_columns"] == ["tenant_id", "result_id"]
            assert ["tenant_id", "result_id", "result_version", "destination", "schema_version"] in [
                constraint["column_names"] for constraint in inspector.get_unique_constraints(Publication.__tablename__)
            ]
            due_index = next(index for index in inspector.get_indexes(Publication.__tablename__) if index["name"] == "ix_l5_publication_due")
            assert due_index["column_names"] == ["state", "next_due_at", "lease_until"]
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
            assert dict(connection.execute(select(AssessmentResult)).mappings().one()) == result_before
            receipt = {
                "receipt_id": "retained-receipt",
                "contribution_id": "retained-publication",
                "admitted_schema": "1.0",
                "consumer_revision": "1",
                "record_refs": [],
            }
            values = dict(
                tenant_id="tenant",
                publication_id="retained-publication",
                result_id="retained-result",
                result_version=1,
                destination="product-context",
                schema_version="1.0",
                contribution_hash="b" * 64,
                payload={"scope": scope, "claims": []},
                state=state,
                receipt=receipt if state != "retry_wait" else None,
                attempts=3,
                revision=7,
                next_due_at=observed_at,
                lease_owner="retained-lease",
                lease_until=observed_at,
                invalidation={"source_refs": [], "reason": "source_access_revoked"} if state == "invalidation_pending" else None,
                invalidation_watermark=1 if state == "invalidation_pending" else 0,
            )
            connection.execute(Publication.__table__.insert().values(**values))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(Publication.__table__.insert().values({**values, "tenant_id": "foreign-tenant"}))
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(Publication.__table__.insert().values({**values, "publication_id": "duplicate-publication"}))
            publication_before = dict(connection.execute(select(Publication)).mappings().one())

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "152_merge_gitlab_prevention")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(Run)).mappings().one()) == run_before
            assert dict(connection.execute(select(AssessmentResult)).mappings().one()) == result_before
            assert dict(connection.execute(select(Publication)).mappings().one()) == publication_before
    finally:
        engine.dispose()
