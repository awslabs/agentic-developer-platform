"""Original paid lifecycle recovery before a cluster or provider call exists."""

# Imported pytest fixtures are intentionally shadowed by fixture parameters.
# ruff: noqa: F811

from dataclasses import replace
from types import SimpleNamespace
import asyncio

import pytest
from harness_jobs.identity import OperationRefused
from harness_jobs.execution import record_intent
from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.leases import read_lease
from harness_jobs.store import OperationStore

from workspace_provisioning.recovery import LifecycleRecovery
from workspace_provisioning.runtime_config import LifecycleRefused

from .postgres_bridge import requires_harness_postgres
from .test_lifecycle_effects_postgres import harness as harness, setup, effects_ddl  # noqa: F401

pytestmark = requires_harness_postgres


async def recovery_case(harness, *, started=False, before_expiry=None, **setup_options):
    operation, context, _ = await setup(harness, record_intent=started, **setup_options)
    lease = operation.grant.lease
    async with harness.connect() as connection:
        await connection.execute(effects_ddl("019_lifecycle_artifacts.py"))
    if before_expiry is not None:
        await before_expiry(operation, context)
    async with harness.connect() as connection:
        await connection.execute(
            "CREATE TABLE workspaces(id text PRIMARY KEY,org_id text,provisioning_operation_id text)"
        )
        await connection.execute(
            "INSERT INTO workspaces VALUES($1,$2,$3)",
            lease.workspace_id,
            lease.org_id,
            lease.operation_id,
        )
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 minute',"
            "runtime_deadline=clock_timestamp()-interval '1 minute' WHERE operation_id=$1",
            lease.operation_id,
        )
    # The pure validator cannot borrow the original execution authority. There
    # are no cloud sessions, credentials, process runners or cluster rows here.
    context.authority = None
    pool = SimpleNamespace(acquire=harness.connect)
    recovery = LifecycleRecovery(
        SimpleNamespace(domain_pool=pool, execution_pool=pool),
        principal=replace(
            operation.grant.principal,
            subject="actual-recovery#1",
            permissions=frozenset({"workspace:recover"}),
        ),
        operation_id=lease.operation_id,
        context=context,
    )
    return operation, recovery


def test_unstarted_onboarding_retries_original_paid_identity_without_cluster(harness):  # noqa: F811
    async def scenario():
        operation, recovery = await recovery_case(harness)
        (result,) = await recovery.run(limit=1)
        assert result.action == "retried"
        assert result.operation_id == operation.grant.lease.operation_id
        async with harness.connect() as connection:
            row = await connection.fetchrow(
                "SELECT o.job_id,o.plan_digest,l.holder,l.fence_token FROM harness_operations o "
                "JOIN harness_operation_leases l USING(operation_id) WHERE o.operation_id=$1",
                result.operation_id,
            )
            assert row["job_id"] == operation.job_id
            assert row["plan_digest"] == operation.plan_digest
            assert row["holder"] is None
            assert row["fence_token"] > operation.grant.lease.fence_token
            assert (
                await connection.fetchval("SELECT count(*) FROM harness_operations")
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_approval_consumption"
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent"
                )
                == 0
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_recovery_settlements"
                )
                == 0
            )

    harness.run(scenario())


@pytest.mark.parametrize("change", ["started", "registration", "policy", "foreign"])
def test_unverified_or_started_lifecycle_never_replays_provider_work(harness, change):  # noqa: F811
    async def scenario():
        operation, recovery = await recovery_case(harness, started=change == "started")
        if change == "foreign":
            recovery.principal = replace(recovery.principal, workspace_id="foreign")
            assert await recovery.run(limit=1) == ()
        else:
            if change == "registration":
                async with harness.connect() as connection:
                    await connection.execute(
                        "UPDATE workspaces SET provisioning_operation_id='other-operation'"
                    )
            if change == "policy":
                recovery.context.policy["runtime"]["actor_role_names"]["installer"] = (
                    "different"
                )
            with pytest.raises((OperationRefused, LifecycleRefused)):
                await recovery.run(limit=1)
        async with harness.connect() as connection:
            current = await read_lease(
                connection, operation_id=operation.grant.lease.operation_id
            )
            assert current.fence_token == operation.grant.lease.fence_token
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_recovery_settlements"
                )
                == 0
            )

    harness.run(scenario())


def test_intent_committing_after_candidate_snapshot_prevents_empty_retry(harness):
    async def scenario():
        operation, recovery = await recovery_case(harness)
        operation_id = operation.grant.lease.operation_id
        intended, selected, committed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        candidate = recovery._paid_candidate

        async def late_writer():
            async with harness.connect() as connection, connection.transaction():
                # Model an executor transaction begun while live. Its lease and
                # intent updates are invisible to the candidate's MVCC snapshot.
                await connection.execute(
                    "UPDATE harness_operation_leases SET expires_at=clock_timestamp()+interval '1 minute',"
                    "runtime_deadline=clock_timestamp()+interval '1 minute' WHERE operation_id=$1",
                    operation_id,
                )
                lease = await read_lease(connection, operation_id=operation_id)
                record = await OperationStore().get(
                    connection, operation.grant.principal, operation_id
                )
                (step,) = admitted_steps(record)
                await record_intent(
                    connection,
                    lease,
                    idempotency_key=step_key(record, step),
                    provider=step.provider,
                    operation_kind=step.operation_kind,
                    target=step.target,
                )
                intended.set()
                await asyncio.wait_for(selected.wait(), timeout=10)
                await connection.execute(
                    "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 minute',"
                    "runtime_deadline=clock_timestamp()-interval '1 minute' WHERE operation_id=$1",
                    operation_id,
                )
            committed.set()

        async def candidate_before_commit():
            found = await candidate()
            assert found == {operation_id}
            selected.set()
            await asyncio.wait_for(committed.wait(), timeout=10)
            return found

        recovery._paid_candidate = candidate_before_commit
        writer = asyncio.create_task(late_writer())
        try:
            await asyncio.wait_for(intended.wait(), timeout=10)
            (result,) = await recovery.run(limit=1)
            await writer
        finally:
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        assert result.action == "deferred"
        assert result.budget_disposition == "retain"
        async with harness.connect() as connection:
            lease = await read_lease(connection, operation_id=operation_id)
            assert lease is not None
            assert lease.fence_token > operation.grant.lease.fence_token
            assert (
                await connection.fetchval(
                    "SELECT stage FROM harness_provider_call_intent WHERE operation_id=$1",
                    operation_id,
                )
                == "intended"
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_recovery_settlements"
                )
                == 0
            )

    harness.run(scenario())
