"""Immutable local evidence survives direct SQL and attempted downgrade."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_local_ledger_is_append_only_even_through_raw_sql():
    filename = Path(__file__).resolve().parents[2] / "alembic/versions/178_security_manual_evidence.py"
    spec = importlib.util.spec_from_file_location("security_migration", filename)
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    assert bridge.revision == "178_security_manual_evidence"
    assert bridge.down_revision == "177_l5_wa_periodic_evidence"
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            bridge.upgrade()
            connection.execute(
                text("INSERT INTO l5_security_evidence_events VALUES ('tenant','id','request','revoked','owner','human','{}','2026-10-31')")
            )
            for statement in ("UPDATE l5_security_evidence_events SET kind='grant'", "DELETE FROM l5_security_evidence_events"):
                with pytest.raises(IntegrityError, match="append-only"):
                    connection.execute(text(statement))
            with pytest.raises(RuntimeError, match="cannot safely be reversed"):
                bridge.downgrade()
            assert connection.execute(text("SELECT kind FROM l5_security_evidence_events")).scalar_one() == "revoked"
    finally:
        engine.dispose()


def test_postgres_head_retains_immutable_security_evidence(pg_url):
    from tests.migrations.conftest_postgres import upgrade
    from tests.migrations.head_contract import GATEWAY_HEAD

    upgrade(pg_url, "head")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            connection.execute(
                text("INSERT INTO l5_security_evidence_events VALUES ('tenant','id','request','revoked','owner','human','{}','2026-10-31')")
            )
            for statement in ("UPDATE l5_security_evidence_events SET kind='grant'", "DELETE FROM l5_security_evidence_events"):
                with pytest.raises(DBAPIError, match="append-only"), connection.begin_nested():
                    connection.execute(text(statement))
            assert connection.scalar(text("SELECT kind FROM l5_security_evidence_events")) == "revoked"
    finally:
        engine.dispose()
