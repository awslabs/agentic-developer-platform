"""Real admission/lease plus domain PostgreSQL: token issuance never grants authority."""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from harness_jobs import OperationStore
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.identity import OperationRefused
from harness_jobs.leases import acquire
from superplane_executor.authority import VerifiedOperation
from superplane_executor.registry import AssignmentRegistry
from tests.conftest import admit_paid, requires_postgres
from tests.test_admission_postgres import principal, request
from handoff_support import granted

pytestmark = requires_postgres


async def prepare(pool, tmp_path):
    org, workspace, cluster, instance = [str(uuid4()) for _ in range(4)]
    actor = principal(org=org, workspace=workspace, subject="invocation#1")
    req = request(
        "controller",
        allocation_id="approved-allocation",
        # This suite injects plan validation to isolate assignment rotation.
        # Explicitly identify its historical V1 plan; production V2 tasks need
        # the independent paid deployment registration tested by the API suite.
        controller_plan=json.dumps({"version": 1}),
        execution_steps=json.dumps(
            [
                {
                    "step_id": "launch",
                    "provider": "aws",
                    "operation_kind": "launch",
                    "target": "capacity",
                }
            ]
        ),
    )
    async with pool.acquire() as connection:
        admitted = await admit_paid(OperationStore(), connection, actor, req)
        lease = await acquire(
            connection,
            operation_id=admitted.record.operation_id,
            holder=actor.subject,
            attempt_id="execution-attempt",
        )
        # Domain table behavior is tested here; the API's migration suite separately
        # executes migration 018, upgrade/downgrade and the full domain query.
        await connection.execute("""
            CREATE TABLE organizations (id uuid PRIMARY KEY, adp_org_id text UNIQUE);
            CREATE TABLE clusters (id uuid PRIMARY KEY,org_id uuid,eks_cluster_arn text,endpoint text,status text);
            CREATE TABLE workspaces (id uuid PRIMARY KEY,org_id uuid,cluster_id uuid,namespace_name text,status text,shared_cluster_id uuid);
            CREATE TABLE observation_leases (scope text PRIMARY KEY,holder text,expires_at timestamptz);
            CREATE TABLE controller_executions (operation_id text PRIMARY KEY,org_id uuid,workspace_id uuid,controller_holder text,assignment json,expires_at timestamptz);
        """)
        await connection.execute(
            "INSERT INTO organizations VALUES ($1::text::uuid,$2)", org, "adp-org"
        )
        await connection.execute(
            "INSERT INTO clusters VALUES ($1::text::uuid,$2::text::uuid,'arn:workspace','https://workspace.example','Ready')",
            cluster,
            org,
        )
        await connection.execute(
            "INSERT INTO workspaces(id,org_id,cluster_id,namespace_name,status) VALUES ($1::text::uuid,$2::text::uuid,$3::text::uuid,'tenant-a','active')",
            workspace,
            org,
            cluster,
        )
        await connection.execute(
            "INSERT INTO observation_leases VALUES ($1,$2,$3)",
            "controller_management/" + org,
            "controller:" + instance,
            datetime.now(UTC) + timedelta(seconds=45),
        )
    verified = VerifiedOperation(
        ExecutionGrant(actor, lease),
        admitted.record.job_id,
        admitted.record.plan_digest,
        admitted.record.request_payload,
        "confirmed",
        4,
        3600,
        5000000,
    )

    class Authority:
        revoked = False

        async def preflight(self, operation):
            if self.revoked:
                raise OperationRefused("provider credential revoked")

        async def resolve(self, operation_id):
            if self.revoked or operation_id != lease.operation_id:
                raise OperationRefused("real authority would refuse this binding")
            return verified

    authority = Authority()
    instance_file = tmp_path / "instance"
    instance_file.write_text(instance)

    async def validate_plan(operation, target):
        assert operation.job_id == admitted.record.job_id
        assert target["workspace_id"] == workspace

    registry = AssignmentRegistry(
        domain_pool=pool,
        execution_pool=pool,
        authority=authority,
        instance_file=instance_file,
        token_dir=tmp_path / "tokens",
        submitter_id="controller",
        validate_plan=validate_plan,
        handoffs=granted(verified),
    )
    return registry, verified, authority


async def test_real_owned_assignment_rotation_and_revocation(pool, tmp_path):
    registry, verified, authority = await prepare(pool, tmp_path)
    name = await registry.publish(verified.grant.lease.operation_id)
    token = (tmp_path / "tokens" / name).read_text()
    assert (await registry.authenticate(token)).lease == verified.grant.lease
    async with pool.acquire() as connection:
        metadata = await connection.fetchval(
            "SELECT assignment::text FROM controller_executions"
        )
        assert token not in metadata
        assert json.loads(metadata)["job_id"] == verified.job_id
    authority.revoked = True
    with pytest.raises(OperationRefused):
        await registry.authenticate(token)
    await registry.refresh([verified.grant.lease.operation_id])
    assert not (tmp_path / "tokens" / name).exists()
    assert not registry.tokens


@pytest.mark.parametrize("change", ["replica", "expired", "workspace", "token"])
async def test_revoked_target_or_replica_cannot_use_previously_issued_token(
    pool, tmp_path, change
):
    registry, verified, _ = await prepare(pool, tmp_path)
    name = await registry.publish(verified.grant.lease.operation_id)
    token = (tmp_path / "tokens" / name).read_text()
    async with pool.acquire() as connection:
        if change == "replica":
            await connection.execute(
                "UPDATE observation_leases SET holder='controller:replacement'"
            )
        elif change == "expired":
            await connection.execute(
                "UPDATE observation_leases SET expires_at=clock_timestamp()-interval '1 second'"
            )
        elif change == "workspace":
            await connection.execute("UPDATE workspaces SET status='retired'")
        else:
            (tmp_path / "tokens" / name).unlink()
    with pytest.raises(OperationRefused):
        await registry.authenticate(token)


async def test_new_service_cannot_reuse_old_tokens_without_current_authority(
    pool, tmp_path
):
    registry, verified, _ = await prepare(pool, tmp_path)
    name = await registry.publish(verified.grant.lease.operation_id)
    old = (tmp_path / "tokens" / name).read_text()
    registry.tokens.clear()  # Represents losing all process memory at restart.
    with pytest.raises(OperationRefused):
        await registry.authenticate(old)
    assert await registry.publish(verified.grant.lease.operation_id) == name
    new = (tmp_path / "tokens" / name).read_text()
    assert old != new
    with pytest.raises(OperationRefused):
        await registry.authenticate(old)
    await registry.authenticate(new)


@pytest.mark.parametrize("change", ["delete", "replace"])
async def test_disk_handoff_withdrawal_during_preflight_refuses_publication(
    pool, tmp_path, change
):
    from handoff_support import handoff_file

    registry, verified, authority = await prepare(pool, tmp_path)
    path = handoff_file(registry, tmp_path / "handoff.json")

    async def withdraw(_operation):
        if change == "delete":
            path.unlink()
        else:
            data = json.loads(path.read_text())
            data["grants"][0]["attempt_id"] = "replacement"
            path.write_text(json.dumps(data))

    authority.preflight = withdraw
    with pytest.raises(OperationRefused):
        await registry.publish(verified.grant.lease.operation_id)
    assert registry.tokens == {}
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT count(*) FROM controller_executions") == 0
        )
