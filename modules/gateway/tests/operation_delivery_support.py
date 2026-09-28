"""Durable lease records for vault authorization tests (the executor schema subset)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import event, text

from src.auth.operation_contract import identity_contract


async def operation_storage(engine):
    @event.listens_for(engine.sync_engine, "connect")
    def clock(connection, _):
        connection.create_function("clock_timestamp", 0, lambda: datetime.now(UTC).replace(tzinfo=None).isoformat(" "))

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: clock(sync.connection.dbapi_connection, None))
        for ddl in (
            "CREATE TABLE harness_operations (operation_id TEXT PRIMARY KEY, job_id TEXT, org_id TEXT, "
            "workspace_id TEXT, state TEXT, request_payload TEXT, plan_digest TEXT, "
            "cancel_requested_at TEXT, cleanup_required BOOLEAN DEFAULT false)",
            "CREATE TABLE harness_operation_leases (operation_id TEXT PRIMARY KEY, org_id TEXT, workspace_id TEXT, "
            "attempt_id TEXT, holder TEXT, fence_token INTEGER, closed_at TEXT, expires_at TEXT, runtime_deadline TEXT)",
            "CREATE TABLE harness_approval_consumption (operation_id TEXT PRIMARY KEY, reservation_state TEXT, "
            "org_id TEXT, workspace_id TEXT, plan_digest TEXT)",
        ):
            await connection.execute(text(ddl))


async def grant_operation(
    session, *, org, workspace, holder, operation="op-1", attempt="att-1", job="job-1", credential="cred-1", service="openai", label="default"
):
    values = dict(
        operation=operation,
        org=org,
        workspace=workspace,
        holder=holder,
        attempt=attempt,
        job=job,
        expiry=(datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None).isoformat(" "),
    )
    await session.execute(
        text(
            "INSERT INTO harness_operations (operation_id, job_id, org_id, workspace_id, state) "
            "VALUES (:operation, :job, :org, :workspace, 'pending')"
        ),
        values,
    )
    await session.execute(
        text(
            "INSERT INTO harness_operation_leases (operation_id, org_id, workspace_id, holder, attempt_id, "
            "fence_token, expires_at, runtime_deadline) "
            "VALUES (:operation, :org, :workspace, :holder, :attempt, 1, :expiry, :expiry)"
        ),
        values,
    )
    await session.execute(
        text(
            "INSERT INTO harness_approval_consumption (operation_id, reservation_state, org_id, workspace_id) "
            "VALUES (:operation, 'confirmed', :org, :workspace)"
        ),
        values,
    )

    await bind_credential(session, credential=credential, service=service, label=label, operation=operation)


async def bind_credential(session, *, credential, service, label, operation="op-1"):
    identity = identity_contract()
    request = identity.OperationRequest(
        action="provision",
        idempotency_key=operation,
        parameters={
            "credential_id": credential,
            "credential_service": service,
            "credential_label": label,
            "provider": service,
            "provider_account_id": "123456789012",
        },
    )
    values = dict(operation=operation, payload=identity.encode_payload(request), digest=identity.payload_digest(request))
    await session.execute(text("UPDATE harness_operations SET request_payload=:payload, plan_digest=:digest WHERE operation_id=:operation"), values)
    await session.execute(text("UPDATE harness_approval_consumption SET plan_digest=:digest WHERE operation_id=:operation"), values)
