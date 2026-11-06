# ruff: noqa: F811 -- imported pytest fixtures are injected by name.
"""Fresh native dispatch honors its paid runtime through bootstrap and lease."""

from datetime import UTC, datetime

import pytest
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest

from tests.internal.test_domain_dispatch_reauthorization import recovery as recovery
from tests.internal.test_paid_domain_operation_postgres import kubernetes as kubernetes
from tests.internal.test_paid_domain_operation_postgres import lifecycle_session_window, start
from tests.internal.test_paid_domain_operation_postgres import paid as paid
from tests.internal.test_paid_domain_operation_postgres import pg_server as pg_server
from tests.internal.test_paid_domain_operation_postgres import pg_url as pg_url
from tests.internal.test_paid_domain_operation_postgres import store as store


def expiry(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.parametrize("approved,expected", [(1800, 1800), (3600, 3600), (7200, 7200), (10800, 10800), (14400, 10800)])
async def test_native_paid_runtime_survives_dispatch_bootstrap_lease_and_renew(paid, approved, expected):
    await lifecycle_session_window(paid, runtime_seconds=approved, approval_seconds=18000)
    async with paid.connect() as connection:
        tables = ("harness_approval_consumption", "operation_budget_reservations", "operation_approvals")
        before = {table: dict(await connection.fetchrow("SELECT * FROM " + table)) for table in tables}
    dispatched, credential = await start(paid)
    deadline = expiry(dispatched["not_after"])
    assert expected - 10 < (deadline - datetime.now(UTC)).total_seconds() <= expected
    leased = await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)
    assert leased.status_code == 200, leased.text
    assert expiry(leased.json()["runtime_deadline"]) == deadline
    assert expiry(leased.json()["not_after"]) == deadline
    renewed = await paid.post("/renew", credential=credential)
    assert renewed.status_code == 200, renewed.text
    assert expiry(renewed.json()["not_after"]) == deadline
    # Redelivery reuses the original mapping, even with a fresh dispatch clock.
    replayed = await paid.post("/dispatch", paid.body, role="producer")
    assert replayed.status_code == 200, replayed.text
    assert replayed.json() == dispatched
    async with paid.connect() as connection:
        assert {table: dict(await connection.fetchrow("SELECT * FROM " + table)) for table in tables} == before
        assert await connection.fetchval("SELECT attempts FROM harness_operation_leases") == 1


async def test_native_dispatch_expires_at_the_earlier_current_approval(paid):
    await lifecycle_session_window(paid, runtime_seconds=10800, approval_seconds=5400)
    async with paid.connect() as connection:
        approved_until = await connection.fetchval("SELECT expires_at FROM operation_approvals")
    dispatched, credential = await start(paid)
    deadline = expiry(dispatched["not_after"])
    assert approved_until.replace(microsecond=0) == deadline
    leased = await paid.post("/lease", {"operation_id": "original-operation"}, credential=credential)
    assert leased.status_code == 200, leased.text
    assert expiry(leased.json()["runtime_deadline"]) == deadline


@pytest.mark.parametrize("action,parameters", [("provision", {}), ("teardown", {"runtime_config_sha256": "a" * 64})])
async def test_controller_and_teardown_do_not_gain_native_provision_runtime(paid, action, parameters):
    await lifecycle_session_window(paid, runtime_seconds=10800, approval_seconds=11700)
    request = OperationRequest(action=action, idempotency_key="approved-key", parameters=parameters)
    async with paid.connect() as connection:
        await connection.execute(
            "UPDATE harness_operations SET action=$1,request_payload=$2,plan_digest=$3", action, encode_payload(request), payload_digest(request)
        )
        await connection.execute("UPDATE harness_approval_consumption SET plan_digest=$1", payload_digest(request))
        await connection.execute("UPDATE operation_approvals SET request_payload=$1,plan_digest=$2", encode_payload(request), payload_digest(request))
    dispatched, _ = await start(paid)
    assert 3590 < (expiry(dispatched["not_after"]) - datetime.now(UTC)).total_seconds() <= 3600


@pytest.mark.parametrize("fault", ["revoked", "expired", "budget", "consumption", "request"])
async def test_long_native_dispatch_still_requires_exact_current_paid_authority(paid, fault):
    await lifecycle_session_window(paid, runtime_seconds=10800, approval_seconds=11700)
    statements = {
        "revoked": "UPDATE operation_approvals SET revoked=true",
        "expired": "UPDATE operation_approvals SET expires_at=clock_timestamp()-interval '1 second'",
        "budget": "UPDATE operation_budget_reservations SET max_runtime_seconds=3600",
        "consumption": "UPDATE harness_approval_consumption SET max_runtime_seconds=14400",
        "request": "UPDATE harness_operations SET request_payload=replace(request_payload,repeat('a',64),repeat('b',64))",
    }
    async with paid.connect() as connection:
        await connection.execute(statements[fault])
    refused = await paid.post("/dispatch", paid.body, role="producer")
    assert refused.status_code == 403, refused.text
    assert "Messages" not in paid.sqs.receive_message(QueueUrl=paid.queue)


async def test_reauthorized_native_execution_keeps_its_existing_recovery_ceiling(recovery):
    paid, body, _ = recovery
    await lifecycle_session_window(paid, runtime_seconds=10800, approval_seconds=11700)
    async with paid.connect() as connection:
        await connection.execute("UPDATE operation_approvals SET expires_at=clock_timestamp()-interval '1 second'")
        await connection.execute("UPDATE harness_dispatch_outbox SET request_payload=(SELECT request_payload FROM harness_operations)")
        await connection.execute(
            "UPDATE operation_reauthorizations SET plan_digest=(SELECT plan_digest FROM harness_operations),"
            "expires_at=clock_timestamp()+interval '4 hours'"
        )
    restored = await paid.post("/recover-undispatched", body, role="producer")
    assert restored.status_code == 200, restored.text
    dispatched, _ = await start(paid)
    assert 3590 < (expiry(dispatched["not_after"]) - datetime.now(UTC)).total_seconds() <= 3600
