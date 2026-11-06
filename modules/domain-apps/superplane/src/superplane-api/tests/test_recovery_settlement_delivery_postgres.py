"""Closed operations retry accounting without reopening a lease or paid attempt."""

# ruff: noqa: F811
import asyncio
from contextlib import asynccontextmanager
import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.recovery_settlement_delivery import RecoverySettlementDelivery
from app.database import Base
from app.models.cloud_account import CloudAccount
from app.models.cluster import Cluster
from app.models.controller_execution import ControllerExecutionAccounting
from app.models.organization import Organization
from app.models.workspace import Workspace
from tests.test_operation_settlement_domains_postgres import separate_owners
from tests.test_operation_settlement_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    pytestmark as pytestmark,
    settlement as settlement,
)


@pytest.fixture
async def delivery(settlement, installation_postgres_url, request):
    _, domain, _, identity, issue = settlement
    receipt = await issue(**getattr(request, "param", {}))
    async with domain.connect() as connection:
        schema = await connection.fetchval("SELECT current_schema()")
    engine = create_async_engine(
        installation_postgres_url,
        connect_args={"server_settings": {"search_path": schema}},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                Organization.__table__,
                CloudAccount.__table__,
                Cluster.__table__,
                Workspace.__table__,
                ControllerExecutionAccounting.__table__,
            ],
        )
    sessions = async_sessionmaker(engine)
    async with sessions() as session:
        session.add(Organization(id=uuid.UUID(identity["org_id"]), name="org"))
        await session.commit()
        session.add(
            Workspace(
                id=uuid.UUID(identity["workspace_id"]),
                org_id=uuid.UUID(identity["org_id"]),
                name="workspace",
                isolation_mode="dedicated",
            )
        )
        await session.commit()
    try:
        async with separate_owners(domain, installation_postgres_url) as (
            budget,
            shared,
        ):
            yield (
                RecoverySettlementDelivery(shared.connect, domain.connect, budget),
                domain,
                shared,
                receipt,
            )
    finally:
        await engine.dispose()


async def snapshot(shared):
    async with shared.connect() as connection:
        return [
            dict(r)
            for r in await connection.fetch("SELECT * FROM harness_operation_leases")
        ]


async def test_closed_receipt_is_delivered_once_across_replicas(delivery):
    sender, domain, shared, receipt = delivery
    before = await snapshot(shared)
    replicas = [
        RecoverySettlementDelivery(shared.connect, domain.connect, sender.ledger)
        for _ in range(4)
    ]
    await asyncio.gather(*(replica.drain_once() for replica in replicas))
    async with shared.connect() as connection:
        assert await connection.fetchval(
            "SELECT delivered_at IS NOT NULL FROM harness_recovery_settlements"
        )
    async with domain.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_settlement_receipts"
            )
            == 1
        )
        projection = json.loads(
            await connection.fetchval(
                "SELECT observation FROM controller_execution_accounting"
            )
        )
        assert projection["receipt_id"] == receipt["receipt_id"]
    assert await snapshot(shared) == before
    assert await sender.drain_once() == ()


async def test_lost_ack_after_budget_commit_retries_without_reexecution(delivery):
    sender, domain, shared, receipt = delivery
    before = await snapshot(shared)
    original_connect = sender.operation_connect

    class LostAck:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, key):
            return getattr(self.connection, key)

        async def execute(self, query, *args):
            if query.startswith("UPDATE harness_recovery_settlements"):
                raise OSError("simulated acknowledgement connection loss")
            return await self.connection.execute(query, *args)

    @asynccontextmanager
    async def lose_ack():
        async with original_connect() as connection:
            yield LostAck(connection)

    sender.operation_connect = lose_ack
    assert await sender.drain_once() == ()
    async with domain.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_settlement_receipts"
            )
            == 1
        )
    async with shared.connect() as connection:
        assert await connection.fetchval(
            "SELECT delivered_at IS NULL FROM harness_recovery_settlements"
        )
    restarted = RecoverySettlementDelivery(
        shared.connect, domain.connect, sender.ledger
    )
    assert await restarted.drain_once() == (receipt["receipt_id"],)
    assert await snapshot(shared) == before


@pytest.mark.parametrize("refusal", ["changed_claim", "foreign_domain"])
async def test_unverified_receipt_is_neither_projected_nor_acknowledged(
    delivery, refusal
):
    sender, domain, shared, _ = delivery
    if refusal == "changed_claim":
        async with shared.connect() as connection:
            await connection.execute(
                "UPDATE harness_operation_leases SET closed_holder='changed'"
            )
    else:
        async with domain.connect() as connection:
            await connection.execute("DELETE FROM workspaces")
    before = await snapshot(shared)
    assert await sender.drain_once() == ()
    async with shared.connect() as connection:
        assert await connection.fetchval(
            "SELECT delivered_at IS NULL FROM harness_recovery_settlements"
        )
    async with domain.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_settlement_receipts"
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_execution_accounting"
            )
            == 0
        )
    assert await snapshot(shared) == before


async def test_bounded_scan_wraps_and_retries_repaired_receipt(delivery):
    sender, _, shared, receipt = delivery
    async with shared.connect() as connection:
        holder = await connection.fetchval(
            "SELECT closed_holder FROM harness_operation_leases"
        )
        await connection.execute(
            "UPDATE harness_operation_leases SET closed_holder='changed'"
        )
    assert await sender.drain_once(limit=1) == ()
    assert sender.cursor == receipt["receipt_id"]
    async with shared.connect() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET closed_holder=$1", holder
        )
    assert await sender.drain_once(limit=1) == ()  # end of page, then wrap
    assert await sender.drain_once(limit=1) == (receipt["receipt_id"],)


async def test_dispatch_pause_defers_actual_closed_receipt(delivery, monkeypatch):
    from app.adapters import operation_dispatch

    sender, domain, shared, receipt = delivery
    dispatcher = operation_dispatch.OperationDispatcher(
        shared.connect, None, domain_connect=domain.connect, settlement_delivery=sender
    )
    monkeypatch.setattr(operation_dispatch, "dispatch_enabled", lambda: False)
    assert await dispatcher.settle_once() == ()
    async with shared.connect() as connection:
        assert await connection.fetchval(
            "SELECT delivered_at IS NULL FROM harness_recovery_settlements"
        )
    monkeypatch.setattr(operation_dispatch, "dispatch_enabled", lambda: True)
    dispatcher.enabled = False
    assert await dispatcher.settle_once() == ()
    dispatcher.enabled = True
    assert await dispatcher.settle_once() == (receipt["receipt_id"],)
