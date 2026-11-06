"""New evidence is empty on upgrade and retained across rollback refusal."""

from datetime import UTC, datetime

import pytest
from l5_services.migrations.versions import l5_wa_periodic_evidence
from l5_services.models.wa_periodic import WAPeriodicCheckpoint, WATransportObservation
from sqlalchemy import create_engine, inspect, select

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT
from tests.migrations.head_contract import GATEWAY_HEAD
from tests.migrations.l5_schema import upgrade_l5_gateway_schema


def test_periodic_evidence_upgrade_never_backfills_and_refuses_discarding_transport():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    bridge = scripts.get_revision("177_l5_wa_periodic_evidence")
    assert bridge.down_revision == "176_l5_schedule_dispatch"
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            upgrade_l5_gateway_schema(connection, through_revision="176_l5_schedule_dispatch")
            assert not inspect(connection).has_table(WATransportObservation.__tablename__)
            with Operations.context(MigrationContext.configure(connection)):
                bridge.module.upgrade()
            assert not connection.execute(select(WATransportObservation)).all()
            assert not connection.execute(select(WAPeriodicCheckpoint)).all()
            connection.execute(
                WATransportObservation.__table__.insert().values(
                    observation_id="observed",
                    send_id="send",
                    tenant_id="tenant-example",
                    configuration_id="configuration",
                    configuration_revision=1,
                    run_id="run",
                    attempt_id="attempt",
                    observed_at=datetime.now(UTC),
                    phase="before_send",
                    facts={"native_effect_count": None},
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
                l5_wa_periodic_evidence.downgrade()
            assert connection.execute(select(WATransportObservation.phase)).scalar_one() == "before_send"
    finally:
        engine.dispose()
