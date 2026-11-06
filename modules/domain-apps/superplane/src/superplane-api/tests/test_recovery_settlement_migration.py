"""A populated receipt table migrates without rewriting accepted accounting."""

# ruff: noqa: F401,F811
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from tests.test_operation_settlement_postgres import (
    installation_postgres_url,
    ledger,
    pytestmark,
    settlement,
)
from tests.test_recovery_settlement_generations_postgres import close_again, resume


async def migrate(url, schema, direction):
    path = (
        Path(__file__).parents[1] / "alembic/versions/056_recovery_settlement_claims.py"
    )
    spec = importlib.util.spec_from_file_location("settlement_claim_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    engine = create_async_engine(
        url, connect_args={"server_settings": {"search_path": schema}}
    )

    def run(connection):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(module, direction)()

    try:
        async with engine.begin() as connection:
            await connection.run_sync(run)
    finally:
        await engine.dispose()


async def test_populated_upgrade_preserves_receipt_and_refuses_lossy_downgrade(
    settlement, installation_postgres_url
):
    budget, connections, _, identity, issue = settlement
    old = await issue(budget="retain", release_permitted=False)
    await budget.deliver_settlement(**old)
    async with connections.connect() as connection:
        schema = await connection.fetchval("SELECT current_schema()")
        before = dict(
            await connection.fetchrow("SELECT * FROM operation_settlement_receipts")
        )
    await migrate(installation_postgres_url, schema, "downgrade")
    await migrate(installation_postgres_url, schema, "upgrade")
    async with connections.connect() as connection:
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM operation_settlement_receipts")
            )
            == before
        )
    await resume(connections, connections, identity["operation_id"])
    claim = await close_again(connections, identity["operation_id"])
    await budget.deliver_settlement(**await issue(claim=claim))
    with pytest.raises(
        DBAPIError, match="cannot discard accepted recovery claim history"
    ):
        await migrate(installation_postgres_url, schema, "downgrade")
    async with connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_settlement_receipts"
            )
            == 2
        )
        assert (
            dict(
                await connection.fetchrow(
                    "SELECT * FROM operation_settlement_receipts WHERE receipt_id=$1",
                    old["receipt_id"],
                )
            )
            == before
        )
