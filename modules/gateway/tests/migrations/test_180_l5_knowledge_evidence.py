"""Actual production upgrades enforce immutable knowledge-use history."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.products.knowledge_evidence_model import KnowledgeUseEvidence
from tests.migrations.conftest_postgres import upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def bridge():
    filename = Path(__file__).resolve().parents[2] / "alembic/versions/180_l5_knowledge_evidence.py"
    spec = importlib.util.spec_from_file_location("knowledge_evidence_migration", filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify(connection):
    assert {item["name"] for item in inspect(connection).get_columns(KnowledgeUseEvidence.__tablename__)} == set(
        KnowledgeUseEvidence.__table__.columns.keys()
    )
    connection.execute(
        KnowledgeUseEvidence.__table__.insert().values(
            tenant_id="tenant",
            id="event",
            operation_id="original-operation",
            evidence_id="prepared-observation",
            kind="observed",
            actor_id="real-human",
            document={},
            event_hash="a" * 64,
            created_at=datetime.now(UTC),
        )
    )
    for statement in ("UPDATE l5_devops_knowledge_evidence SET kind='completed'", "DELETE FROM l5_devops_knowledge_evidence"):
        with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
            connection.execute(text(statement))
    with pytest.raises(RuntimeError, match="The L5 release retains owner authorization and qualification history and cannot safely be reversed"):
        bridge().downgrade()
    assert connection.scalar(text("SELECT kind FROM l5_devops_knowledge_evidence")) == "observed"


def test_sqlite_knowledge_evidence_migration_is_append_only():
    migration = bridge()
    assert migration.down_revision == "179_l5_knowledge_effects"
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            verify(connection)
    finally:
        engine.dispose()


def test_postgres_gateway_upgrade_adds_evidence_without_metadata_creation(pg_url):
    upgrade(pg_url, "179_l5_knowledge_effects")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert not inspect(connection).has_table(KnowledgeUseEvidence.__tablename__)
        upgrade(pg_url, "head")
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            verify(connection)
    finally:
        engine.dispose()
