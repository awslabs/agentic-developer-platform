"""Verify the vault query against real executor migrations and lease acquisition."""

import sys
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.auth.vault_delivery import DeliveryRefusedError, OperationBinding, _validate_operation_binding
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

# Test-only cross-package contract check; Gateway runtime imports no executor code.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "harness/jobs"))
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest  # noqa: E402
from harness_jobs.leases import acquire  # noqa: E402
from harness_jobs.schema import apply  # noqa: E402


@pytest.fixture
async def authority(pg_url):  # noqa: F811
    connection = await asyncpg.connect(pg_url)
    try:
        await apply(connection)
        await connection.execute("""
            INSERT INTO harness_operations
                (operation_id, attempt_id, job_id, org_id, workspace_id, action,
                 idempotency_key, plan_digest, request_payload, contract_version, state)
            VALUES ('operation', 'admission-attempt', 'job', 'org', 'workspace',
                    'provision', 'key', 'digest', '{}', 'v1', 'pending')
        """)
        await connection.execute("""
            INSERT INTO harness_approval_consumption
                (approval_id, operation_id, org_id, workspace_id, plan_digest,
                 requester, approved_by, max_resource_units, max_runtime_seconds,
                 max_cost_micros, reservation_state)
            VALUES ('approval', 'operation', 'org', 'workspace', 'digest',
                    'requester', 'approver', 1, 60, 100, 'confirmed')
        """)
        request = OperationRequest(
            action="provision",
            idempotency_key="key",
            parameters={
                "credential_id": "cred-1",
                "credential_service": "openai",
                "credential_label": "default",
                "provider": "openai",
                "provider_account_id": "123456789012",
            },
        )
        await connection.execute("UPDATE harness_operations SET request_payload=$1, plan_digest=$2", encode_payload(request), payload_digest(request))
        await connection.execute("UPDATE harness_approval_consumption SET plan_digest=$1", payload_digest(request))
        lease = await acquire(connection, operation_id="operation", holder="invocation#9", attempt_id="execution-attempt")
    finally:
        await connection.close()
    engine = create_async_engine(to_async_url(pg_url))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False), lease
    finally:
        await engine.dispose()


@pytest.mark.parametrize("change", ["cancel", "expired", "holder", "attempt", "budget"])
async def test_actual_executor_lease_is_required_and_rechecked(authority, change):
    factory, lease = authority
    binding = OperationBinding("operation", "execution-attempt", "job", "org", "workspace", "openai", "123456789012")
    async with factory() as reader, factory() as writer:
        assert (await _validate_operation_binding(reader, binding, "invocation#9"))[0] == lease.fence_token
        statement = {
            "cancel": "UPDATE harness_operations SET cancel_requested_at=clock_timestamp()",
            "expired": "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second'",
            "holder": "UPDATE harness_operation_leases SET holder='other-invocation#1'",
            "attempt": "UPDATE harness_operation_leases SET attempt_id='superseded'",
            "budget": "UPDATE harness_approval_consumption SET reservation_state='retained'",
        }[change]
        await writer.execute(text(statement))
        await writer.commit()
        with pytest.raises(DeliveryRefusedError):
            await _validate_operation_binding(reader, binding, "invocation#9")
