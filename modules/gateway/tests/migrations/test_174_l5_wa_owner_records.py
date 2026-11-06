"""Gateway upgrades install every WA owner record without granting or losing consent."""

from datetime import UTC, datetime, timedelta

import pytest
from l5_services.models import AssessmentResult, Configuration, ConfigurationVersion, Run
from l5_services.models.base import Base as L5Base
from l5_services.models.wa_qualification import WellArchitectedQualification
from l5_services.models.wa_reuse_authorization import WellArchitectedReuseAuthorization
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql import JSONB

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL
from tests.migrations.l5_schema import upgrade_l5_gateway_schema


@pytest.mark.parametrize("parent", [None, "172_story_task_board", "173_l5_execution_scope"])
def test_owner_records_upgrade_preserves_history_and_never_backfills_consent(request, parent):
    url = request.getfixturevalue("pg_url") if parent else "sqlite:///:memory:"
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    bridge = scripts.get_revision("174_l5_wa_owner_records")
    assert bridge.down_revision == "173_l5_execution_scope"
    assert scripts.get_heads() == [GATEWAY_HEAD]
    if parent:
        upgrade(url, parent)
    engine = create_engine(url)
    now = datetime(2026, 10, 31, tzinfo=UTC)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    try:
        with engine.begin() as connection:
            if not parent:
                connection.execute(text("PRAGMA foreign_keys=ON"))
                upgrade_l5_gateway_schema(connection, through_revision="168_merge_security_wa")
            assert not inspect(connection).has_table(WellArchitectedReuseAuthorization.__tablename__)
            assert not inspect(connection).has_table(WellArchitectedQualification.__tablename__)
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
                AssessmentResult.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    run_id="run-example",
                    result_id="result-example",
                    version=1,
                    payload={"review_mode": "source_only", "retained": True},
                )
            )
            before = {
                model: connection.execute(select(model)).mappings().all() for model in (Configuration, ConfigurationVersion, Run, AssessmentResult)
            }

        if parent:
            upgrade(url)
            upgrade(url)
        else:
            with engine.begin() as connection:
                upgrade_l5_gateway_schema(connection, after_revision="168_merge_security_wa")

        with engine.begin() as connection:
            inspector = inspect(connection)
            assert set(L5Base.metadata.tables) <= set(inspector.get_table_names())
            for table in L5Base.metadata.sorted_tables:
                assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.columns.keys())
            for model, rows in before.items():
                assert connection.execute(select(model)).mappings().all() == rows
            for model in (WellArchitectedReuseAuthorization, WellArchitectedQualification):
                assert connection.execute(select(model)).all() == []
                table = model.__table__
                assert set(inspector.get_pk_constraint(table.name)["constrained_columns"]) == set(table.primary_key.columns.keys())
                assert {entry["name"] for entry in inspector.get_indexes(table.name)} == {index.name for index in table.indexes}
                assert len(inspector.get_foreign_keys(table.name)) == 1
            if parent:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
                columns = {column["name"]: column for column in inspector.get_columns(WellArchitectedReuseAuthorization.__tablename__)}
                assert isinstance(columns["scope"]["type"], JSONB)
                columns = {column["name"]: column for column in inspector.get_columns(WellArchitectedQualification.__tablename__)}
                assert isinstance(columns["selection"]["type"], JSONB)
            connection.execute(
                WellArchitectedReuseAuthorization.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    authorization_id="authorization-example",
                    result_id="result-example",
                    actor_id="owner-example",
                    scope=scope,
                    scope_digest="b" * 64,
                    request_digest="c" * 64,
                    revision=1,
                    expires_at=now + timedelta(hours=1),
                    revoked_at=now,
                )
            )
            connection.execute(
                WellArchitectedQualification.__table__.insert().values(
                    tenant_id=scope["tenant_id"],
                    experiment_id="experiment-example",
                    configuration_id="config-example",
                    configuration_revision=1,
                    actor_id="owner-example",
                    mode="observations",
                    selection={"run_ids": ["run-example"]},
                    selection_digest="d" * 64,
                    request_digest="e" * 64,
                    revision=1,
                    created_at=now,
                    expires_at=now + timedelta(hours=1),
                    revoked_at=now,
                )
            )
            retained = {
                model: connection.execute(select(model)).mappings().all()
                for model in (WellArchitectedReuseAuthorization, WellArchitectedQualification)
            }

        if parent:
            upgrade(url)
            refused = run_alembic(url, "downgrade", "173_l5_execution_scope")
            assert refused.returncode != 0 and HEAD_DOWNGRADE_REFUSAL in refused.stderr
        else:
            with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
                with pytest.raises(
                    RuntimeError, match="The L5 release retains owner authorization and qualification history and cannot safely be reversed"
                ):
                    bridge.module.downgrade()
        with engine.connect() as connection:
            for model, rows in {**before, **retained}.items():
                assert connection.execute(select(model)).mappings().all() == rows
    finally:
        engine.dispose()
