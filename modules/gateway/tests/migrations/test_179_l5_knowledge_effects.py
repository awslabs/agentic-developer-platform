"""Production Gateway Alembic creates and preserves knowledge effect history."""

from datetime import UTC, datetime

import pytest
from l5_services.models.configuration import Configuration
from l5_services.models.knowledge_effect import KnowledgeEffect, KnowledgeOwnership
from l5_services.models.setup import SetupIntent
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import DBAPIError

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_gateway_upgrade_installs_real_knowledge_tables_without_metadata_creation(pg_url):
    upgrade(pg_url, "178_security_manual_evidence")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert not inspect(connection).has_table(KnowledgeEffect.__tablename__)
        upgrade(pg_url, "head")
        scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
        assert scripts.get_heads() == [GATEWAY_HEAD]
        bridge = scripts.get_revision("179_l5_knowledge_effects")
        assert bridge.down_revision == "178_security_manual_evidence"
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            assert "dispatch_attempt_limit" in {item["name"] for item in inspect(connection).get_columns("l5_service_setup_work")}
            for model in (KnowledgeEffect, KnowledgeOwnership):
                assert {item["name"] for item in inspect(connection).get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
                assert not connection.execute(select(model)).all()
            connection.execute(
                Configuration.__table__.insert().values(
                    tenant_id="tenant",
                    configuration_id="configuration",
                    product_id="product",
                    project_id="project",
                    service_id="devops",
                    relationship_revision="1",
                    owner_id="owner",
                    current_revision=1,
                    handoff_state="linked",
                )
            )
            connection.execute(
                SetupIntent.__table__.insert().values(
                    tenant_id="tenant",
                    operation_id="operation",
                    product_id="product",
                    project_id="project",
                    service_id="devops",
                    actor_id="owner",
                    idempotency_key="original",
                    configuration_id="configuration",
                    scope={},
                    request={},
                    authorization={},
                    intent_hash="a" * 64,
                )
            )
            connection.execute(
                KnowledgeEffect.__table__.insert().values(
                    tenant_id="tenant",
                    operation_id="operation",
                    source_key="b" * 64,
                    sequence=1,
                    phase="planned",
                    work_id="work",
                    lease_owner="worker",
                    fence=1,
                    created_at=datetime.now(UTC),
                    payload={},
                    event_hash="c" * 64,
                )
            )
            for statement in ("UPDATE l5_devops_knowledge_effects SET phase='ready'", "DELETE FROM l5_devops_knowledge_effects"):
                with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
                    connection.execute(text(statement))
            with pytest.raises(
                RuntimeError, match="The L5 release retains owner authorization and qualification history and cannot safely be reversed"
            ):
                bridge.module.downgrade()
            assert connection.scalar(select(KnowledgeEffect.phase)) == "planned"
    finally:
        engine.dispose()
