"""Joining service histories installs setup fences without losing pending work."""

from datetime import UTC, datetime

import pytest
from l5_services.models.configuration import Configuration
from l5_services.models.setup import SetupIntent, SetupWork
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_security_and_wa_histories_have_one_additive_gateway_head():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("168_merge_security_wa").down_revision == (
        "164_l5_work_delegation_parent",
        "167_l5_wa_results",
    )
    assert scripts.get_revision("164_l5_work_delegation_parent").down_revision == "163_merge_connector_cost"
    assert scripts.get_revision("167_l5_wa_results").down_revision == "166_l5_wa_generation"


@pytest.mark.parametrize("initial_revision", ["164_l5_work_delegation_parent", "167_l5_wa_results"])
def test_join_preserves_pending_setup_from_either_parent(pg_url, initial_revision):
    upgrade(pg_url, initial_revision)
    engine = create_engine(pg_url)
    scope = {"tenant_id": "tenant-example", "product_id": "product-example", "project_id": "project-example"}
    try:
        with engine.begin() as connection:
            connection.execute(
                Configuration.__table__.insert().values(
                    **scope,
                    configuration_id="config-example",
                    service_id="security",
                    relationship_revision="1",
                    owner_id="owner-example",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection.execute(
                SetupIntent.__table__.insert().values(
                    **scope,
                    operation_id="setup-example",
                    service_id="security",
                    actor_id="owner-example",
                    idempotency_key="setup-key",
                    configuration_id="config-example",
                    scope=scope,
                    request={},
                    authorization={},
                    intent_hash="a" * 64,
                )
            )
            pending = Table(SetupWork.__tablename__, MetaData(), autoload_with=connection)
            connection.execute(
                pending.insert().values(
                    tenant_id=scope["tenant_id"],
                    work_id="work-example",
                    operation_id="setup-example",
                    generation=1,
                    due_at=datetime(2026, 10, 28, tzinfo=UTC),
                    attempts=2,
                )
            )
            before = dict(connection.execute(select(pending)).mappings().one())
            assert "fence" not in before and "lease_owner" not in before

        upgrade(pg_url)
        upgrade(pg_url)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert dict(connection.execute(select(SetupWork)).mappings().one()) == {
                **before,
                "lease_owner": None,
                "fence": 0,
                "dispatch_attempt_limit": None,
            }
            inspector = inspect(connection)
            assert "parent_delegation_id" in {column["name"] for column in inspector.get_columns("credential_service_delegations")}
            assert inspector.has_table("l5_service_wa_usage")
    finally:
        engine.dispose()
