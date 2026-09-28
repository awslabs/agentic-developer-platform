"""Run durable audit request paths against independent PostgreSQL connections."""

# ruff: noqa: F401,F811 -- imported tests and fixtures are intentionally collected.
import tempfile

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from src.shared.models.base import Base
from tests.admin.test_admin_audit import session, session_factory
from tests.admin.test_admin_audit_durability import (
    test_connection_receipt_uses_resolved_tenant,
    test_external_operation_has_preexisting_intent_and_honest_outcome,
    test_route_permission_denial_is_durable_and_target_unchanged,
    test_success_and_correlation_survive_request_teardown,
    test_terminal_sink_failure_exposes_pending_operation,
    test_uncommitted_sql_is_rolled_back_on_failure,
)


@pytest.fixture(scope="module")
def audit_postgres():
    pgserver = pytest.importorskip("pgserver", reason="Disposable PostgreSQL requires the Python 3.12 test dependency")

    # Never consume an environment-provided database or production configuration.
    with tempfile.TemporaryDirectory(prefix="s13-audit-pg-", dir="/tmp") as directory:
        server = pgserver.get_server(directory)
        try:
            yield server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)
        finally:
            server.cleanup()


@pytest.fixture
async def engine(audit_postgres):
    engine = create_async_engine(audit_postgres, connect_args={"server_settings": {"statement_timeout": "5000"}})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.drop_all)
        await engine.dispose()
