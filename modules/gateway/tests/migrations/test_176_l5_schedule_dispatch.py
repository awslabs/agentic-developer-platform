"""Install schedule state without enabling work or discarding earlier history."""

from datetime import UTC, datetime

import pytest
from l5_services.migrations.versions import l5_schedule_dispatch
from l5_services.models.schedule import EventObservation, ScheduleState
from sqlalchemy import create_engine, inspect, select

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT
from tests.migrations.head_contract import GATEWAY_HEAD
from tests.migrations.l5_schema import upgrade_l5_gateway_schema


def test_schedule_upgrade_starts_empty_and_refuses_to_discard_observations():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    bridge = scripts.get_revision("176_l5_schedule_dispatch")
    assert bridge.down_revision == "175_l5_setup_authority"
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            upgrade_l5_gateway_schema(connection, through_revision="175_l5_setup_authority")
            assert not inspect(connection).has_table(ScheduleState.__tablename__)
            with Operations.context(MigrationContext.configure(connection)):
                bridge.module.upgrade()
            assert not connection.execute(select(ScheduleState)).all()
            assert not connection.execute(select(EventObservation)).all()
            connection.execute(
                EventObservation.__table__.insert().values(
                    observation_id="retained-observation",
                    tenant_id="tenant-example",
                    idempotency_key="a" * 64,
                    observed_at=datetime.now(UTC),
                    kind="received",
                    state="rejected",
                    reason="trigger_disabled",
                )
            )
            with pytest.raises(
                RuntimeError, match="The L5 release retains owner authorization and qualification history and cannot safely be reversed"
            ):
                bridge.module.downgrade()
            with (
                Operations.context(MigrationContext.configure(connection)),
                pytest.raises(RuntimeError, match="downgrade would discard retained data"),
            ):
                l5_schedule_dispatch.downgrade()
            assert connection.execute(select(EventObservation.reason)).scalar_one() == "trigger_disabled"
    finally:
        engine.dispose()
