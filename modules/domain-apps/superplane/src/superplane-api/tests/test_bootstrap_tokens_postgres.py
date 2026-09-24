"""Real SQL token rotation, claim projection, cancellation and lease expiry fences."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

from fastapi import HTTPException
import pytest

from app.models.organization import Organization
from app.services.bootstrap_observation import provisional_targets
from app.services.bootstrap_tokens import (
    authenticate_bootstrap_token,
    issue_bootstrap_read_token,
    token_digest,
)
from tests.test_installation_postgres import (  # noqa: F401
    admin_membership,
    bootstrap_database,
    installation_postgres_url,
    isolated_database,
)


@pytest.fixture
async def token_runtime(bootstrap_database, isolated_database):  # noqa: F811
    import asyncpg

    module = Path(__file__).resolve().parents[3]
    jobs = module.parents[1] / "harness" / "jobs"
    sys.path.insert(0, str(jobs))
    from harness_jobs import apply
    from harness_jobs.execution_rpc import ExecutionGrant
    from harness_jobs.identity import (
        OperationRequest,
        ResolvedPrincipal,
        REQUIRED_PERMISSION,
    )
    from harness_jobs.leases import read_lease
    from harness_jobs.store import OperationStore
    from superplane_bootstrap.state import claim_fingerprint

    bootstrap, factory, config, _ = bootstrap_database
    bootstrap_config = {key: config[key] for key in ("org_id", "adp_org_id", "origin")}
    await bootstrap.bootstrap(
        {**bootstrap_config, "control_plane_only": True},
        "verified-test-token",
        membership_reader=admin_membership,
    )
    _, _, url, _, schema, _ = isolated_database

    @asynccontextmanager
    async def connect():
        connection = await asyncpg.connect(
            url.replace("postgresql+asyncpg://", "postgresql://", 1),
            server_settings={"search_path": schema},
        )
        try:
            yield connection
        finally:
            await connection.close()

    workspace = str(uuid.uuid4())
    principal = ResolvedPrincipal(
        org_id=config["org_id"],
        workspace_id=workspace,
        subject="bootstrap-worker-fixture",
        permissions=frozenset({REQUIRED_PERMISSION}),
    )
    now = datetime.now(UTC)
    claim_token = "fixture-reservation-token-" + "x" * 32
    claim = claim_fingerprint(claim_token)
    async with connect() as connection:
        await apply(connection)
        admitted = await OperationStore().admit(
            connection,
            principal,
            OperationRequest(
                action="provision", idempotency_key="bootstrap-token-fixture"
            ),
        )
        operation = admitted.record.operation_id
        # Synthetic held-worker fixture, read back through the real lease model.
        # No provider call or spend authority is granted by these read-token tests.
        await connection.execute(
            "INSERT INTO harness_operation_leases (operation_id,org_id,workspace_id,holder,fence_token,attempt_id,expires_at,acquired_at,runtime_deadline,attempts) VALUES ($1,$2,$3,$4,1,$5,$6,$7,$8,1)",
            operation,
            principal.org_id,
            workspace,
            principal.subject,
            admitted.record.attempt_id,
            now + timedelta(minutes=1),
            now,
            now + timedelta(minutes=5),
        )
        identity = {
            "workspace_id": workspace,
            "org_id": config["org_id"],
            "cluster_arn": "arn:aws:eks:us-east-1:000000000002:cluster/fixture",
            "namespace": "fixture",
        }
        observation = {
            **identity,
            "operation_id": operation,
            "registration_claim": claim,
            "endpoint": "https://fixture.example.invalid",
        }
        await connection.execute(
            "INSERT INTO workspace_bootstrap_reservations(workspace_id,state,identity_json,attempt_token) VALUES($1,'reserved',$2,$3)",
            workspace,
            json.dumps(identity),
            claim_token,
        )
        await connection.execute(
            "INSERT INTO workspace_bootstrap_authority(workspace_id,generation,operation_id,org_id,cluster_arn,claim,plan_json,progress_json,revoked) VALUES($1,$2,$3,$4,$5,$6,'{}',$7,false)",
            workspace,
            "a" * 64,
            operation,
            principal.org_id,
            identity["cluster_arn"],
            claim,
            json.dumps(
                {
                    "phase": "active",
                    "component_inventory_complete": True,
                    "management_observation": observation,
                }
            ),
        )
        lease = await read_lease(connection, operation_id=operation)
    grant = ExecutionGrant(principal, lease)

    def request(token):
        return SimpleNamespace(
            headers={"authorization": token},
            app=SimpleNamespace(
                state=SimpleNamespace(
                    trust_composition=SimpleNamespace(operation_connect=connect)
                )
            ),
        )

    yield SimpleNamespace(
        connect=connect,
        grant=grant,
        factory=factory,
        request=request,
        org_id=uuid.UUID(config["org_id"]),
        claim=claim,
    )


async def test_replacement_revokes_only_prior_hash_and_caps_expiry_to_database_lease(
    token_runtime,
):
    runtime = token_runtime
    extended = replace(
        runtime.grant,
        lease=replace(
            runtime.grant.lease, expires_at=datetime.now(UTC) + timedelta(days=1)
        ),
    )
    first = await issue_bootstrap_read_token(connect=runtime.connect, grant=extended)
    second = await issue_bootstrap_read_token(
        connect=runtime.connect, grant=runtime.grant
    )
    assert first != second
    async with runtime.connect() as connection:
        row = await connection.fetchrow("SELECT * FROM workspace_bootstrap_read_tokens")
        assert row["token_hash"] == token_digest(second)
        assert first not in str(dict(row)) and second not in str(dict(row))
        assert row["expires_at"] == runtime.grant.lease.expires_at
    async with runtime.factory() as db:
        with pytest.raises(HTTPException) as error:
            await authenticate_bootstrap_token(runtime.request(first), db)
        assert error.value.status_code == 401
        accepted = await authenticate_bootstrap_token(runtime.request(second), db)
        assert accepted.operation_id == runtime.grant.lease.operation_id


async def test_projection_disappears_on_cancellation_and_token_cannot_be_reissued(
    token_runtime,
):
    runtime = token_runtime
    token = await issue_bootstrap_read_token(
        connect=runtime.connect, grant=runtime.grant
    )
    async with runtime.factory() as db:
        org = await db.get(Organization, runtime.org_id)
        projected = await provisional_targets(db, org=org, connect=runtime.connect)
        assert len(projected) == 1 and projected[0]["provisional_observation"] is True
        assert projected[0]["cluster_id"] == ""
        assert "execution_assignments" not in projected[0]
    async with runtime.connect() as connection:
        await connection.execute(
            "UPDATE harness_operations SET cancel_requested_at=clock_timestamp() WHERE operation_id=$1",
            runtime.grant.lease.operation_id,
        )
    async with runtime.factory() as db:
        org = await db.get(Organization, runtime.org_id)
        assert await provisional_targets(db, org=org, connect=runtime.connect) == []
        with pytest.raises(HTTPException):
            await authenticate_bootstrap_token(runtime.request(token), db)
    with pytest.raises(HTTPException):
        await issue_bootstrap_read_token(connect=runtime.connect, grant=runtime.grant)


async def test_replaced_reservation_claim_cannot_reuse_previous_operation_observation(
    token_runtime,
):
    runtime = token_runtime
    async with runtime.connect() as connection:
        await connection.execute(
            "UPDATE workspace_bootstrap_reservations SET attempt_token=$1 WHERE workspace_id=$2",
            "replacement-fixture-token",
            runtime.grant.lease.workspace_id,
        )
    async with runtime.factory() as db:
        org = await db.get(Organization, runtime.org_id)
        assert await provisional_targets(db, org=org, connect=runtime.connect) == []
    with pytest.raises(HTTPException):
        await issue_bootstrap_read_token(connect=runtime.connect, grant=runtime.grant)


async def test_readiness_can_rotate_read_token_after_installer_revocation(
    token_runtime,
):
    runtime = token_runtime
    first = await issue_bootstrap_read_token(
        connect=runtime.connect, grant=runtime.grant
    )
    async with runtime.connect() as connection:
        await connection.execute(
            "UPDATE workspace_bootstrap_authority SET revoked=true, "
            "progress_json=(progress_json::jsonb || $1::jsonb)::text",
            json.dumps({"phase": "revoked", "retain_workspace": True}),
        )
    second = await issue_bootstrap_read_token(
        connect=runtime.connect, grant=runtime.grant
    )
    assert first != second
    async with runtime.factory() as db:
        org = await db.get(Organization, runtime.org_id)
        assert len(await provisional_targets(db, org=org, connect=runtime.connect)) == 1
        assert await authenticate_bootstrap_token(runtime.request(second), db)
    # Rollback that relinquishes the workspace cannot regain even read authority.
    async with runtime.connect() as connection:
        await connection.execute(
            "UPDATE workspace_bootstrap_authority SET "
            "progress_json=(progress_json::jsonb || $1::jsonb)::text",
            json.dumps({"retain_workspace": False}),
        )
    with pytest.raises(HTTPException):
        await issue_bootstrap_read_token(connect=runtime.connect, grant=runtime.grant)
    async with runtime.factory() as db:
        org = await db.get(Organization, runtime.org_id)
        assert await provisional_targets(db, org=org, connect=runtime.connect) == []
