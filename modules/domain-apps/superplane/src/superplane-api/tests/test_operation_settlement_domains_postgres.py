"""Separate pools and schemas must preserve settlement fencing and atomic effects."""


# Ruff cannot distinguish imported fixtures from parameter shadowing.
# ruff: noqa: F811

import asyncio
from contextlib import asynccontextmanager
import uuid

import pytest
from harness_jobs.admission import BudgetDenied, BudgetUnavailable

from app.adapters.harness_connection import HarnessConnections
from app.adapters.operation_budget_ledger import OperationBudgetLedger
from tests.test_operation_settlement_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    pytestmark as pytestmark,
    retirement_settlement as retirement_settlement,
    settlement as settlement,
)
from tests.test_operation_budget_ledger_postgres import row


@asynccontextmanager
async def separate_owners(domain, url):
    schema = "settlement_shared_" + uuid.uuid4().hex
    async with domain.connect() as connection:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
        tables = await connection.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname=current_schema() "
            "AND tablename LIKE 'harness_%'"
        )
        for table in tables:
            name = table["tablename"]
            assert name.replace("_", "").isalnum()
            await connection.execute(f'ALTER TABLE "{name}" SET SCHEMA "{schema}"')
    shared = HarnessConnections(
        url.replace("postgresql+asyncpg://", "postgresql://", 1),
        {
            "ssl": "disable",
            "server_settings": {"search_path": schema, "statement_timeout": "10000"},
        },
    )
    await shared.open()
    try:
        yield (
            OperationBudgetLedger(domain.connect, operation_connect=shared.connect),
            shared,
        )
    finally:
        await shared.aclose()
        async with domain.connect() as connection:
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.mark.parametrize("budget_disposition", ["release", "retain", "settle"])
async def test_separate_schema_receipt_redelivery_commits_once(
    settlement,
    installation_postgres_url,
    budget_disposition,  # noqa: F811
):
    _, domain, _, identity, issue = settlement
    receipt = await issue(budget=budget_disposition)
    async with separate_owners(domain, installation_postgres_url) as (budget, shared):
        assert await budget.deliver_settlement(**receipt) == receipt["receipt_id"]
        # A new adapter and concurrent deliveries model a lost acknowledgement.
        restarted = OperationBudgetLedger(
            domain.connect, operation_connect=shared.connect
        )
        results = await asyncio.gather(
            *(restarted.deliver_settlement(**receipt) for _ in range(8))
        )
        assert results == [receipt["receipt_id"]] * 8
        reservation = await row(domain, identity["job_id"], identity["attempt_id"])
        assert reservation["state"] == (
            "released" if budget_disposition == "release" else "retained"
        )
        async with domain.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM operation_settlement_receipts"
                )
                == 1
            )


@pytest.mark.parametrize("failure", ["closed_claim", "receiver_insert"])
async def test_separate_schema_refusal_or_write_failure_changes_no_budget(
    settlement,
    installation_postgres_url,
    failure,  # noqa: F811
):
    _, domain, _, identity, issue = settlement
    receipt = await issue()
    async with separate_owners(domain, installation_postgres_url) as (budget, shared):
        if failure == "closed_claim":
            async with shared.connect() as connection:
                await connection.execute(
                    "UPDATE harness_operation_leases SET closed_holder='other'"
                )
        else:
            async with domain.connect() as connection:
                await connection.execute(
                    "ALTER TABLE operation_settlement_receipts ADD CHECK (disposition='never')"
                )
        with pytest.raises(
            BudgetDenied if failure == "closed_claim" else BudgetUnavailable
        ):
            await budget.deliver_settlement(**receipt)
        assert (await row(domain, identity["job_id"], identity["attempt_id"]))[
            "state"
        ] == "confirmed"
        async with domain.connect() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM operation_settlement_receipts"
                )
                == 0
            )


@pytest.mark.parametrize("exposure", ["none", "unknown"])
async def test_separate_schema_retirement_links_original_allocation(
    retirement_settlement,
    installation_postgres_url,
    exposure,  # noqa: F811
):
    _, domain, _, source, retired, issue = retirement_settlement
    receipt = await issue(exposure=exposure)
    async with separate_owners(domain, installation_postgres_url) as (budget, _):
        await budget.deliver_settlement(**receipt)
        await budget.deliver_settlement(**receipt)
        for identity in (source, retired):
            assert (await row(domain, identity["job_id"], identity["attempt_id"]))[
                "state"
            ] == ("released" if exposure == "none" else "retained")
