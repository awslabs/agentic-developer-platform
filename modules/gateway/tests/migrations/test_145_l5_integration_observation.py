"""Upgrade existing L5 receipts without rewriting history or dropping recovery data."""

from l5_services.models import DevopsSpaceIntent
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB

from tests.migrations.conftest_postgres import run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_integration_receipt_upgrade_preserves_existing_and_new_observations(pg_url):
    upgrade(pg_url, "144_merge_l5_flow_reads")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            intents = Table("l5_devops_space_intents", MetaData(), autoload_with=connection)
            assert "integration_observation" not in intents.columns
            connection.execute(
                intents.insert().values(
                    tenant_id="tenant",
                    operation_id="retained-operation",
                    selection={"product_id": "product"},
                    configuration_revision=1,
                    intent_hash="a" * 64,
                    state="ready",
                    native_space_id="retained-space",
                    resource_observation={"space_id": "retained-space"},
                    owned_trigger_ids=["retained-trigger"],
                    resume_trigger_ids=[],
                    active_executions=[],
                )
            )
            before = dict(connection.execute(select(intents)).mappings().one())

        upgrade(pg_url)
        with engine.begin() as connection:
            column = next(column for column in inspect(connection).get_columns(intents.name) if column["name"] == "integration_observation")
            assert isinstance(column["type"], JSONB) and column["nullable"]
            after = dict(connection.execute(select(DevopsSpaceIntent)).mappings().one())
            assert {key: after[key] for key in before} == before
            assert after["integration_observation"] is None
            observation = {"integration_id": "retained-integration", "phase": "associated"}
            connection.execute(DevopsSpaceIntent.__table__.update().values(integration_observation=observation))

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "144_merge_l5_flow_reads")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            after = dict(connection.execute(select(DevopsSpaceIntent)).mappings().one())
            assert {key: after[key] for key in before} == before
            assert after["integration_observation"] == observation
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
    finally:
        engine.dispose()
