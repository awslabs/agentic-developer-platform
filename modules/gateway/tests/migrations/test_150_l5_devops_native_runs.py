"""Gateway upgrades retain admitted work and install scoped native observations."""

from datetime import UTC, datetime

import pytest
from l5_services.models.configuration import Configuration, ConfigurationVersion
from l5_services.models.devops_event import NativeEventReceipt
from l5_services.models.devops_observation import NativeRunIdentity
from l5_services.models.receipt import RunReceipt
from l5_services.models.run import Run
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError

from tests.migrations.conftest_postgres import run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


@pytest.mark.parametrize("service_id", ["devops", "security"])
def test_gateway_upgrade_preserves_admission_and_enables_native_identity(pg_url, service_id):
    upgrade(pg_url, "149_merge_l5_project_budgets")
    engine = create_engine(pg_url)
    deadline = datetime(2026, 10, 28, tzinfo=UTC)
    scope = {"tenant_id": "tenant", "product_id": "product", "project_id": "project"}
    try:
        with engine.begin() as connection:
            connection.execute(
                Configuration.__table__.insert().values(
                    tenant_id="tenant",
                    configuration_id="config",
                    product_id="product",
                    project_id="project",
                    service_id=service_id,
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
            old_runs = Table(Run.__tablename__, MetaData(), autoload_with=connection)
            values = dict(
                tenant_id="tenant",
                run_id="admitted-run",
                product_id="product",
                project_id="project",
                service_id=service_id,
                configuration_id="config",
                configuration_revision=1,
                binding_id="binding",
                binding_generation=1,
                trigger_id="manual",
                revision=1,
                input={"service_id": service_id},
                admission={"scope": scope},
                state="running",
                provider_state="running",
                deadline_at=deadline,
            )
            connection.execute(old_runs.insert().values(**values))
            connection.execute(
                RunReceipt.__table__.insert().values(
                    **scope,
                    service_id=service_id,
                    actor_id="owner",
                    operation="submit",
                    idempotency_key="admitted-key",
                    request_hash="a" * 64,
                    run_id="admitted-run",
                    expires_at=deadline,
                )
            )
            before = dict(connection.execute(select(old_runs)).mappings().one())
            receipt_before = dict(connection.execute(select(RunReceipt)).mappings().one())

        upgrade(pg_url)
        with engine.begin() as connection:
            after = dict(connection.execute(select(Run)).mappings().one())
            assert {key: after[key] for key in before} == before
            assert after["origin"] == "adp_submitted"
            assert after["scope"] is None and after["next_check_at"] is None
            inspector = inspect(connection)
            assert next(column for column in inspector.get_columns(Run.__tablename__) if column["name"] == "admission")["nullable"]
            for model in (NativeRunIdentity, NativeEventReceipt):
                assert {column["name"] for column in inspector.get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
            connection.execute(
                Run.__table__.insert().values(
                    {
                        **values,
                        "run_id": "native-run",
                        "service_id": "devops",
                        "origin": "native",
                        "admission": None,
                        "scope": scope,
                        "next_check_at": deadline,
                    }
                )
            )
            identity = dict(tenant_id="tenant", connection_id="connection", space_id="space", execution_id="execution", run_id="native-run")
            connection.execute(NativeRunIdentity.__table__.insert().values(**identity))
            connection.execute(
                NativeEventReceipt.__table__.insert().values(
                    tenant_id="tenant",
                    connection_id="connection",
                    space_id="space",
                    event_id="event",
                    event_hash="b" * 64,
                    run_id="native-run",
                )
            )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    NativeEventReceipt.__table__.insert().values(
                        tenant_id="foreign-tenant",
                        connection_id="connection",
                        space_id="space",
                        event_id="event",
                        event_hash="b" * 64,
                        run_id="native-run",
                    )
                )

        upgrade(pg_url)
        refused = run_alembic(pg_url, "downgrade", "149_merge_l5_project_budgets")
        assert refused.returncode != 0
        assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(Run).where(Run.run_id == "admitted-run")).mappings().one()) == after
            assert dict(connection.execute(select(RunReceipt)).mappings().one()) == receipt_before
            assert dict(connection.execute(select(NativeRunIdentity)).mappings().one()) == identity
            native = connection.execute(select(Run).where(Run.run_id == "native-run")).mappings().one()
            assert native["admission"] is None and native["origin"] == "native"
            assert native["scope"] == scope and native["next_check_at"] == deadline
            assert connection.execute(select(NativeEventReceipt)).mappings().one()["tenant_id"] == "tenant"
    finally:
        engine.dispose()
