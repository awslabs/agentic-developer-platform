"""Recovery generations retain history without letting old receipts affect a new run."""

# ruff: noqa: F401,F811
import asyncio
import json

import pytest
from asyncpg.exceptions import RaiseError, UniqueViolationError
from harness_jobs.admission import BudgetDenied
from harness_jobs.schema import apply, downgrade

from tests.test_operation_budget_ledger_postgres import row
from tests.test_operation_settlement_domains_postgres import separate_owners
from tests.test_operation_settlement_postgres import (
    installation_postgres_url,
    ledger,
    pytestmark,
    settlement,
)
from tests.test_recovery_settlement_delivery_postgres import delivery


async def resume(shared, domain, operation_id):
    """Seed the post-reauthorization boundary; its proof belongs to recovery tests."""
    async with shared.connect() as connection:
        await connection.execute(
            "UPDATE harness_operations SET state='running' WHERE operation_id=$1",
            operation_id,
        )
        await connection.execute(
            "UPDATE harness_operation_leases SET fence_token=4,closed_at=NULL,"
            "closed_holder=NULL,closed_attempt_id=NULL WHERE operation_id=$1",
            operation_id,
        )
    async with domain.connect() as connection:
        await connection.execute(
            "UPDATE operation_budget_reservations SET state='confirmed'"
        )


async def close_again(shared, operation_id):
    claim = {
        "holder": "recovery#2",
        "attempt_id": "recovery-attempt-2",
        "fence_token": 5,
    }
    async with shared.connect() as connection:
        await connection.execute(
            "UPDATE harness_operations SET state='failed' WHERE operation_id=$1",
            operation_id,
        )
        await connection.execute(
            "UPDATE harness_operation_leases SET fence_token=5,closed_at=now(),"
            "closed_holder=$2,closed_attempt_id=$3 WHERE operation_id=$1",
            operation_id,
            claim["holder"],
            claim["attempt_id"],
        )
    return claim


async def test_acknowledged_history_does_not_resettle_reopened_budget(settlement):
    budget, connections, _, identity, issue = settlement
    old = await issue(budget="retain", release_permitted=False)
    assert await budget.deliver_settlement(**old) == old["receipt_id"]
    await resume(connections, connections, identity["operation_id"])
    assert (
        await asyncio.gather(*(budget.deliver_settlement(**old) for _ in range(4)))
        == [old["receipt_id"]] * 4
    )
    assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
        "state"
    ] == "confirmed"


async def test_unaccepted_old_receipt_cannot_settle_a_reopened_claim(settlement):
    budget, connections, _, identity, issue = settlement
    old = await issue(budget="retain", release_permitted=False)
    await resume(connections, connections, identity["operation_id"])
    with pytest.raises(BudgetDenied):
        await budget.deliver_settlement(**old)
    assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
        "state"
    ] == "confirmed"
    async with connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_settlement_receipts"
            )
            == 0
        )


@pytest.mark.parametrize("second_budget", ["release", "retain", "settle"])
async def test_new_claim_settles_once_and_old_receipt_stays_immutable(
    settlement, second_budget
):
    budget, connections, _, identity, issue = settlement
    old = await issue(budget="retain", release_permitted=False)
    await budget.deliver_settlement(**old)
    async with connections.connect() as connection:
        old_shared = dict(
            await connection.fetchrow("SELECT * FROM harness_recovery_settlements")
        )
        old_domain = dict(
            await connection.fetchrow("SELECT * FROM operation_settlement_receipts")
        )
    await resume(connections, connections, identity["operation_id"])
    claim = await close_again(connections, identity["operation_id"])
    new = await issue(claim=claim, budget=second_budget)
    assert (
        await asyncio.gather(*(budget.deliver_settlement(**new) for _ in range(4)))
        == [new["receipt_id"]] * 4
    )
    assert await budget.deliver_settlement(**old) == old["receipt_id"]
    expected = "released" if second_budget == "release" else "retained"
    assert (await row(connections, identity["job_id"], identity["attempt_id"]))[
        "state"
    ] == expected
    async with connections.connect() as connection:
        assert (
            dict(
                await connection.fetchrow(
                    "SELECT * FROM harness_recovery_settlements WHERE receipt_id=$1",
                    old["receipt_id"],
                )
            )
            == old_shared
        )
        assert (
            dict(
                await connection.fetchrow(
                    "SELECT * FROM operation_settlement_receipts WHERE receipt_id=$1",
                    old["receipt_id"],
                )
            )
            == old_domain
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_settlement_receipts"
            )
            == 2
        )
        with pytest.raises(RaiseError, match="immutable"):
            await connection.execute(
                "DELETE FROM harness_recovery_settlements WHERE receipt_id=$1",
                old["receipt_id"],
            )


async def test_v17_upgrade_preserves_history_and_fences_each_claim(settlement):
    _, connections, _, identity, issue = settlement
    async with connections.connect() as connection:
        await downgrade(connection, target=16)
    old = await issue(budget="retain")
    async with connections.connect() as connection:
        before = dict(
            await connection.fetchrow("SELECT * FROM harness_recovery_settlements")
        )
        assert await apply(connection, target=17) == 17
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_recovery_settlements")
            )
            == before
        )
    with pytest.raises(UniqueViolationError):
        await issue(budget="settle")  # Same claim, different payload.
    claim = await close_again(connections, identity["operation_id"])
    await issue(claim=claim, budget="retain")
    async with connections.connect() as connection:
        with pytest.raises(RaiseError, match="per-claim"):
            await downgrade(connection, target=16)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_recovery_settlements"
            )
            == 2
        )
        assert (
            dict(
                await connection.fetchrow(
                    "SELECT * FROM harness_recovery_settlements WHERE receipt_id=$1",
                    old["receipt_id"],
                )
            )
            == before
        )


@pytest.mark.parametrize(
    "delivery", [{"budget": "retain", "release_permitted": False}], indirect=True
)
async def test_delayed_sender_cannot_overwrite_reopened_accounting(delivery):
    sender, domain, shared, receipt = delivery
    async with shared.connect() as connection:
        stale = await connection.fetchrow("SELECT * FROM harness_recovery_settlements")
    assert await sender.drain_once() == (receipt["receipt_id"],)
    await resume(shared, domain, receipt["operation_id"])
    marker = {"source": "new-execution", "fence_token": 4}
    async with domain.connect() as connection:
        await connection.execute(
            "UPDATE controller_execution_accounting SET observation=$1::json",
            json.dumps(marker),
        )
    # An already-fetched old outbox row may arrive after the native resume.
    assert await sender._deliver(stale)
    async with domain.connect() as connection:
        assert (
            json.loads(
                await connection.fetchval(
                    "SELECT observation FROM controller_execution_accounting"
                )
            )
            == marker
        )
    assert (await row(domain, receipt["job_id"], receipt["attempt_id"]))[
        "state"
    ] == "confirmed"
