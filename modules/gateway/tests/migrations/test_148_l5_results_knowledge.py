"""The gateway installs current L5 tables while retaining published knowledge."""

from l5_services.models import DevopsKnowledgePublication, Finding, ResultDetail, Triage
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB

from tests.migrations.conftest_postgres import run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_upgrade_installs_scoped_results_and_retains_knowledge(pg_url):
    upgrade(pg_url, "147_merge_l5_list_indexes")
    engine = create_engine(pg_url)
    models = (ResultDetail, Finding, Triage, DevopsKnowledgePublication)
    try:
        with engine.connect() as connection:
            for model in models:
                assert not inspect(connection).has_table(model.__tablename__)

        upgrade(pg_url)
        with engine.begin() as connection:
            inspector = inspect(connection)
            for model in models:
                assert {column["name"] for column in inspector.get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
            for model, local_key, parent_table, parent_key in (
                (ResultDetail, "result_id", "l5_service_results", "result_id"),
                (Finding, "result_id", "l5_service_results", "result_id"),
                (Triage, "finding_id", "l5_service_findings", "finding_id"),
            ):
                foreign_key = inspector.get_foreign_keys(model.__tablename__)[0]
                assert foreign_key["constrained_columns"] == ["tenant_id", local_key]
                assert foreign_key["referred_table"] == parent_table
                assert foreign_key["referred_columns"] == ["tenant_id", parent_key]
            assert ["tenant_id", "result_id", "kind", "native_id"] in [
                constraint["column_names"] for constraint in inspector.get_unique_constraints(Finding.__tablename__)
            ]
            for model, column_name in ((ResultDetail, "payload"), (Finding, "payload"), (Triage, "decision"), (DevopsKnowledgePublication, "intent")):
                column = next(column for column in inspector.get_columns(model.__tablename__) if column["name"] == column_name)
                assert isinstance(column["type"], JSONB) and not column["nullable"]
            publications = DevopsKnowledgePublication.__table__
            connection.execute(
                publications.insert().values(
                    tenant_id="tenant",
                    space_id="retained-space",
                    source_key="a" * 64,
                    product_id="product",
                    project_id="project",
                    operation_id="retained-operation",
                    intent={"source_revision": "retained-revision"},
                    state="published",
                    asset_id="retained-asset",
                    asset_version=1,
                )
            )
            before = dict(connection.execute(select(publications)).mappings().one())

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "147_merge_l5_list_indexes")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(publications)).mappings().one()) == before
            for model in models:
                assert inspect(connection).has_table(model.__tablename__)
    finally:
        engine.dispose()
