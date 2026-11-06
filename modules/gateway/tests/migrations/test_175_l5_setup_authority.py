"""Continuation migration preserves accepted setup and never backfills consent."""

from datetime import UTC, datetime

import pytest
from l5_services.migrations.versions import l5_setup_authority_continuations
from l5_services.models.configuration import Configuration
from l5_services.models.setup import BindingOperation, SetupAuthorityContinuation, SetupIntent, SetupWork
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT
from tests.migrations.head_contract import GATEWAY_HEAD
from tests.migrations.l5_schema import upgrade_l5_gateway_schema


def test_continuation_upgrade_preserves_original_setup_and_retains_history():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    bridge = scripts.get_revision("175_l5_setup_authority")
    assert bridge.down_revision == "174_l5_wa_owner_records"
    assert scripts.get_heads() == [GATEWAY_HEAD]
    engine = create_engine("sqlite:///:memory:")
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    try:
        with engine.begin() as connection:
            connection.execute(text("PRAGMA foreign_keys=ON"))
            upgrade_l5_gateway_schema(connection, through_revision="174_l5_wa_owner_records")
            assert not inspect(connection).has_table(SetupAuthorityContinuation.__tablename__)
            connection.execute(
                Configuration.__table__.insert().values(
                    **scope,
                    configuration_id="config-example",
                    service_id="devops",
                    relationship_revision="1",
                    owner_id="owner-example",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection.execute(
                SetupIntent.__table__.insert().values(
                    **scope,
                    operation_id="operation-example",
                    service_id="devops",
                    actor_id="owner-example",
                    idempotency_key="original-create-request",
                    configuration_id="config-example",
                    scope=scope,
                    request={"action": "create", "configuration_revision": 1},
                    binding_generation=1,
                    authorization={"delegation_id": "expired-consent"},
                    intent_hash="a" * 64,
                )
            )
            connection.execute(
                BindingOperation.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    operation_id="operation-example",
                    action="create",
                    state="accepted",
                    revision=1,
                    gaps=[],
                )
            )
            connection.execute(
                SetupWork.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    work_id="work-example",
                    operation_id="operation-example",
                    generation=1,
                    due_at=datetime(2026, 10, 31, tzinfo=UTC),
                    attempts=1,
                )
            )
            # Compare the actual pre-175 schema. Current ORM models can include
            # columns added by later migrations that this upgrade does not run.
            historical = MetaData()
            tables = [
                Table(model.__tablename__, historical, autoload_with=connection)
                for model in (Configuration, SetupIntent, BindingOperation, SetupWork)
            ]
            before = {table: connection.execute(select(table)).mappings().all() for table in tables}
            with Operations.context(MigrationContext.configure(connection)):
                bridge.module.upgrade()
            assert connection.execute(select(SetupAuthorityContinuation)).all() == []
            connection.execute(
                SetupAuthorityContinuation.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    operation_id="operation-example",
                    sequence=1,
                    idempotency_key="explicit-continuation",
                    request_hash="b" * 64,
                    receipt={"retained": "owner-consent-receipt"},
                    receipt_hash="c" * 64,
                )
            )
            retained = connection.execute(select(SetupAuthorityContinuation)).mappings().all()
            with pytest.raises(
                RuntimeError, match="The L5 release retains owner authorization and qualification history and cannot safely be reversed"
            ):
                bridge.module.downgrade()
            with (
                Operations.context(MigrationContext.configure(connection)),
                pytest.raises(RuntimeError, match="downgrade would discard retained data"),
            ):
                l5_setup_authority_continuations.downgrade()
            for table, rows in before.items():
                assert connection.execute(select(table)).mappings().all() == rows
            assert connection.execute(select(SetupAuthorityContinuation)).mappings().all() == retained
    finally:
        engine.dispose()
