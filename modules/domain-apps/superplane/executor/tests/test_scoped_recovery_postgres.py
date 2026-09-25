"""Recovery consequences through real PostgreSQL and the real inventory finalizer.

Cloud transports are offline. No fabricated cleanup assessment or successful request
status substitutes for inventory, sealing, report attestation or capacity retirement.
"""

import asyncio
import json

# Ruff treats imported pytest fixtures as unused names when parameters shadow them.
# ruff: noqa: F811
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from harness_jobs.execution import (
    BudgetDisposition,
    CallOutcome,
    CallStage,
    ProviderCallRefused,
)
from harness_jobs.execution_rpc import ExecutionRPCServer
from harness_jobs.identity import OperationRefused
from harness_jobs.leases import fence_expired_lease, read_lease
from harness_jobs.recovery import SweepResult
from harness_jobs.recovery_grant import RecoveryGrant, lock_recovery_grant
from superplane_executor.inventory import Finalizer
from superplane_executor.plan import Plan
from superplane_executor.recovery import (
    ScopedRecovery,
    authorized_operation,
    journalled_operations,
)
from superplane_executor.recovery_inventory import RecoveryFinalizer
from superplane_executor.recovery_observation import observe_request
from tests.conftest import requires_postgres
from tests.test_admission_postgres import principal

from test_lifecycle_postgres import system as system  # noqa: F401

pytestmark = requires_postgres


class Ledger:
    """Receiver port recorder. Real budget receiver/dedup tests belong to its owner."""

    def __init__(self):
        self.calls = []

    async def deliver_settlement(self, **receipt):
        self.calls.append(receipt)
        return receipt["receipt_id"]


def pools(registry):
    """The trusted provider surface ScopedRecovery needs, from the live registry."""
    return SimpleNamespace(
        domain_pool=registry.domain_pool, execution_pool=registry.execution_pool
    )


async def scope(pool, operation_id):
    """The authenticated principal whose tenant recovery may act for."""
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT org_id, workspace_id FROM controller_provider_requests "
            "WHERE operation_id=$1 LIMIT 1",
            operation_id,
        )
    return replace(
        principal(
            org=row["org_id"], workspace=row["workspace_id"], subject="recovery-1"
        ),
        permissions=frozenset({"workspace:recover"}),
    )


async def expire(pool, operation_id):
    """Make the lease lapse, exactly as a killed controller would leave it."""
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases "
            "SET expires_at=clock_timestamp()-interval '1 minute', "
            "    runtime_deadline=clock_timestamp()-interval '1 minute' "
            "WHERE operation_id=$1",
            operation_id,
        )


async def start_provisioning(admit, server, cloud=None):
    """Leave a provision abandoned mid-launch, as a killed controller would.

    With `cloud`, the launch is submitted and its durable handle journalled, but the
    process dies before observing the result. Real capacity may exist while the call
    is still `INTENDED` -- precisely the state recovery has to resolve, and the reason
    a release may not be assumed. Without `cloud`, the launch completes normally.
    """
    if cloud is not None:
        cloud.lose_status_response = True
    operation, token = await admit("provision")
    await server.dispatch(
        {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
    )
    return operation


async def start_retirement(admit, server, cloud):
    """Leave a *complete* one-step plan abandoned mid-removal.

    Settlement requires the whole admitted plan to be confirmed: a provision whose
    first step succeeded is a confirmed prefix, and the shared engine deliberately
    offers it for bounded continuation instead of settling it. Teardown is one
    `delete_cluster` step, so observing it resolves the entire plan -- which is what
    makes this the shape that exercises settlement, accounting and ledger delivery.
    """
    _, token = await admit("provision")
    for step in ("1", "2", "3", "4"):
        await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": step}}
        )
    cloud.lose_status_response = True
    operation, token = await admit("teardown")
    await server.dispatch(
        {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
    )
    return operation


async def intended(pool, operation_id):
    """The calls still in flight for this operation."""
    async with pool.acquire() as connection:
        return await connection.fetch(
            "SELECT idempotency_key FROM harness_provider_call_intent "
            "WHERE operation_id=$1 AND stage=$2",
            operation_id,
            CallStage.INTENDED.value,
        )


async def accounting_for(pool, operation_id):
    async with pool.acquire() as connection:
        raw = await connection.fetchval(
            "SELECT observation::text FROM controller_execution_accounting "
            "WHERE operation_id=$1",
            operation_id,
        )
    return None if raw is None else json.loads(raw)


class ObservationBackend(Ledger):
    """Offline provider observations with current database claim authentication."""

    def __init__(self, registry, provider, operation, actor):
        super().__init__()
        self.registry, self.provider = registry, provider
        self.operation, self.actor = operation, actor
        self.inventory_reads = 0
        self.finalization_errors = []

    async def recovery_scope(self):
        return self.actor

    async def resolve_recovery(self, claim):
        async with self.provider.execution_pool.acquire() as connection:
            current = await read_lease(connection, operation_id=claim.operation_id)
            subject = await connection.fetchval(
                "SELECT subject FROM harness_recovery_claim_bindings WHERE operation_id=$1 AND fence_token=$2",
                claim.operation_id,
                claim.fence_token,
            )
        if current != claim or subject != self.actor.subject:
            raise OperationRefused("recovery claim binding refused")
        return replace(self.operation, grant=RecoveryGrant(self.actor, claim))

    async def observe(self, claim, key, *_):
        operation = await self.resolve_recovery(claim)
        async with self.provider.domain_pool.acquire() as connection:
            journal = await connection.fetchrow(
                "SELECT request_id,operation_kind FROM controller_provider_requests WHERE idempotency_key=$1 AND operation_id=$2",
                key,
                claim.operation_id,
            )
            target = await connection.fetchrow(
                "SELECT w.id::text AS workspace_id,w.org_id::text AS domain_org_id,w.namespace_name AS namespace,"
                "c.id::text AS cluster_id,c.eks_cluster_arn AS cluster_arn,c.endpoint FROM workspaces w "
                "JOIN clusters c ON c.id=w.cluster_id AND c.org_id=w.org_id "
                "WHERE w.id::text=$1 AND w.org_id::text=$2",
                claim.workspace_id,
                claim.org_id,
            )
        outcome, reference = await observe_request(
            self.provider,
            operation,
            Plan.read(operation, dict(target)),
            target=dict(target),
            call={"idempotency_key": key},
            authorize=lambda: self.resolve_recovery(claim),
            **dict(journal),
        )
        await self.resolve_recovery(claim)
        return CallOutcome(outcome), "offline status", reference

    async def inventory(self, claim, allocation_id):
        self.inventory_reads += 1
        operation = await self.resolve_recovery(claim)
        async with self.provider.domain_pool.acquire() as connection:
            target = dict(
                await connection.fetchrow(
                    "SELECT w.id::text AS workspace_id,w.org_id::text AS domain_org_id,w.namespace_name AS namespace,"
                    "c.id::text AS cluster_id,c.eks_cluster_arn AS cluster_arn,c.endpoint FROM workspaces w "
                    "JOIN clusters c ON c.id=w.cluster_id WHERE w.id::text=$1",
                    claim.workspace_id,
                )
            )
            calls = await connection.fetch(
                "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
                claim.operation_id,
            )
        finalizer = Finalizer(self.provider, self.registry)
        plan = Plan.read(operation, target)
        resources = await finalizer.discover(operation, target, plan, calls)
        result = []
        for resource in resources.values():
            observed = await finalizer.observe(operation, target, plan, resource)
            result.append(
                {
                    "resource_id": resource.resource_id,
                    "provider": resource.provider,
                    "provider_reference": resource.provider_reference,
                    "kind": resource.kind,
                    "operation_keys": list(resource.operation_keys),
                    "presence": observed.presence.value,
                    "provider_state": observed.provider_state,
                    "detail": observed.detail,
                }
            )
        return result


async def retiring(system):
    pool, admit, server, cloud, _, registry, _ = system
    operation = await start_retirement(admit, server, cloud)
    cloud.lose_status_response = False
    actor = await scope(pool, operation.grant.lease.operation_id)
    await expire(pool, operation.grant.lease.operation_id)
    backend = ObservationBackend(
        registry, server._after_step.provider, operation, actor
    )
    recovery = ScopedRecovery(
        pools(registry),
        principal=actor,
        observe_claim=backend.observe,
        finalize=RecoveryFinalizer(pools(registry), backend),
        ledger=backend,
    )
    prepare = recovery._prepare

    async def diagnosed_prepare(lease, calls):
        try:
            return await prepare(lease, calls)
        except Exception as error:
            # Production deliberately hides provider exception text. Preserve its
            # chain only in this offline test so a remote CI refusal is diagnosable.
            while error is not None:
                backend.finalization_errors.append(f"{type(error).__name__}: {error}")
                error = error.__context__
            raise

    recovery._prepare = diagnosed_prepare
    return operation, backend, recovery


async def assert_open(pool, operation_id):
    async with pool.acquire() as connection:
        state, closed = await connection.fetchrow(
            "SELECT o.state,l.closed_at FROM harness_operations o JOIN harness_operation_leases l USING(operation_id) WHERE operation_id=$1",
            operation_id,
        )
        assert state not in {"succeeded", "failed", "unknown", "cancelled"}
        assert closed is None
        assert not await connection.fetchval(
            "SELECT count(*) FROM harness_recovery_settlements WHERE operation_id=$1",
            operation_id,
        )


async def test_foreign_scope_and_unprivileged_principal_cannot_advance_claim(system):
    pool, admit, server, _, _, registry, _ = system
    operation = await start_provisioning(admit, server)
    lease = operation.grant.lease
    actor = await scope(pool, lease.operation_id)
    assert await journalled_operations(
        pool, actor, candidates=[lease.operation_id, "foreign"]
    ) == {lease.operation_id}
    foreign = replace(actor, workspace_id=str(uuid4()))
    with pytest.raises(OperationRefused, match="outside"):
        await authorized_operation(pool, foreign, lease.operation_id)
    with pytest.raises(OperationRefused, match="workspace:recover"):
        ScopedRecovery(pools(registry), principal=operation.grant.principal)
    await expire(pool, lease.operation_id)
    assert await ScopedRecovery(pools(registry), principal=foreign).run() == ()
    async with pool.acquire() as connection:
        assert (
            await read_lease(connection, operation_id=lease.operation_id)
        ).fence_token == lease.fence_token


async def test_successful_stop_without_finalizer_never_closes_or_retires(system):
    operation, backend, recovery = await retiring(system)
    recovery.finalize = None
    (result,) = await recovery.run()
    assert result.action == "deferred"
    assert not backend.calls
    await assert_open(system[0], operation.grant.lease.operation_id)
    async with system[0].acquire() as connection:
        assert (
            await connection.fetchval("SELECT state FROM controller_capacity")
            != "retired"
        )


async def test_paid_task_recovers_lease_lost_before_first_provider_journal(system):
    pool, admit, _, cloud, _, registry, _ = system
    operation, _ = await admit("provision")
    lease = operation.grant.lease
    actor = replace(
        operation.grant.principal,
        subject="recovery-run#1",
        permissions=frozenset({"workspace:recover"}),
    )
    await expire(pool, lease.operation_id)
    assert (
        await journalled_operations(pool, actor, candidates=[lease.operation_id])
        == frozenset()
    )
    # A generic scanner still has no authority over unjournalled work.
    assert await ScopedRecovery(pools(registry), principal=actor).run() == ()
    (result,) = await ScopedRecovery(
        pools(registry), principal=actor, operation_id=lease.operation_id
    ).run()
    assert result.action == "retried"
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT holder FROM harness_operation_leases WHERE operation_id=$1",
                lease.operation_id,
            )
            is None
        )
        assert (
            await connection.fetchval(
                "SELECT fence_token FROM harness_operation_leases WHERE operation_id=$1",
                lease.operation_id,
            )
            > lease.fence_token
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_call_intent"
            )
            == 0
        )
    assert cloud.launches == 0


async def test_paid_recovery_selector_cannot_advance_a_foreign_workspace_lease(system):
    pool, admit, _, _, _, registry, _ = system
    operation, _ = await admit("provision")
    lease = operation.grant.lease
    actor = replace(
        operation.grant.principal,
        workspace_id=str(uuid4()),
        permissions=frozenset({"workspace:recover"}),
    )
    await expire(pool, lease.operation_id)
    assert (
        await ScopedRecovery(
            pools(registry), principal=actor, operation_id=lease.operation_id
        ).run()
        == ()
    )
    async with pool.acquire() as connection:
        assert (
            await read_lease(connection, operation_id=lease.operation_id)
        ).fence_token == lease.fence_token


async def test_paid_recovery_selector_refuses_mismatched_approval_digest(system):
    pool, admit, _, _, _, registry, _ = system
    operation, _ = await admit("provision")
    lease = operation.grant.lease
    actor = replace(
        operation.grant.principal, permissions=frozenset({"workspace:recover"})
    )
    await expire(pool, lease.operation_id)
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE harness_approval_consumption SET plan_digest='foreign-plan' WHERE operation_id=$1",
            lease.operation_id,
        )
    assert (
        await ScopedRecovery(
            pools(registry), principal=actor, operation_id=lease.operation_id
        ).run()
        == ()
    )
    async with pool.acquire() as connection:
        assert (
            await read_lease(connection, operation_id=lease.operation_id)
        ).fence_token == lease.fence_token


async def test_recovery_grant_preserves_actual_subject_and_cannot_execute(system):
    pool, admit, server, cloud, _, _, _ = system
    operation = await start_provisioning(admit, server)
    lease = operation.grant.lease
    actor = await scope(pool, lease.operation_id)
    await expire(pool, lease.operation_id)
    async with pool.acquire() as connection:
        takeover = await fence_expired_lease(
            connection, operation_id=lease.operation_id, recovery_principal=actor
        )
        grant = RecoveryGrant(actor, takeover.lease)
        assert grant.principal.subject != grant.lease.holder
        assert not grant.principal.may_provision
        async with connection.transaction():
            assert await lock_recovery_grant(connection, grant)
            assert not await lock_recovery_grant(
                connection,
                replace(grant, principal=replace(actor, subject="another-run")),
            )
            assert not await lock_recovery_grant(
                connection,
                replace(
                    grant, lease=replace(grant.lease, attempt_id="another-attempt")
                ),
            )

    async def authenticate(_):
        return grant

    launches = cloud.launches
    rpc = ExecutionRPCServer(
        connect=pool.acquire,
        provider_call=server._provider_call,
        authenticate=authenticate,
    )
    with pytest.raises(ProviderCallRefused, match="credential refused"):
        await rpc.dispatch(
            {
                "token": "recovery-selector",
                "method": "execute_step",
                "arguments": {"step_id": "1"},
            }
        )
    assert cloud.launches == launches
    await expire(pool, lease.operation_id)
    async with pool.acquire() as connection:
        async with connection.transaction():
            assert not await lock_recovery_grant(connection, grant)


async def test_inventory_refuses_another_authenticated_recovery_subject(system):
    operation, backend, recovery = await retiring(system)
    resolve = backend.resolve_recovery

    async def wrong_subject(claim):
        resolved = await resolve(claim)
        grant = replace(
            resolved.grant,
            principal=replace(resolved.grant.principal, subject="another-run"),
        )
        return replace(resolved, grant=grant)

    backend.resolve_recovery = wrong_subject
    (result,) = await recovery.run()
    assert result.action == "deferred"
    assert backend.inventory_reads >= 3, backend.finalization_errors
    assert not backend.calls
    await assert_open(system[0], operation.grant.lease.operation_id)


@pytest.mark.parametrize("leaked", [False, True])
async def test_actual_inventory_decides_retirement_and_terminal_settlement(
    system, leaked
):
    operation, backend, recovery = await retiring(system)
    system[3].leaked_volume = leaked
    launches = system[3].launches
    (result,) = await recovery.run()
    assert backend.inventory_reads >= (1 if leaked else 3), backend.finalization_errors
    assert system[3].launches == launches
    if leaked:
        assert result.action == "deferred"
        assert not backend.calls
        await assert_open(system[0], operation.grant.lease.operation_id)
    else:
        assert result.action == "succeeded", backend.finalization_errors
        (receipt,) = backend.calls
        assert receipt["accounting"]["inventory_complete"] is True
        assert receipt["accounting"]["may_mark_released"] is True
        assert receipt["accounting"]["claim"]["attempt_id"] != receipt["attempt_id"]
        async with system[0].acquire() as connection:
            assert (
                await connection.fetchval("SELECT state FROM controller_capacity")
                == "retired"
            )
            assert await connection.fetchval(
                "SELECT closed_at IS NOT NULL FROM harness_operation_leases WHERE operation_id=$1",
                operation.grant.lease.operation_id,
            )
        assert await recovery.deliver_pending() == ()
        assert len(backend.calls) == 1


async def test_receipt_insert_and_terminal_close_roll_back_together(system):
    operation, backend, recovery = await retiring(system)
    settled = recovery._settled
    entered = []

    async def crash(connection, lease, result):
        await settled(connection, lease, result)
        entered.append(lease)
        raise RuntimeError("crash after receipt insert")

    recovery._settled = crash
    with pytest.raises(RuntimeError, match="after receipt"):
        await recovery.run()
    assert entered and backend.inventory_reads >= 3
    await assert_open(system[0], operation.grant.lease.operation_id)
    assert await recovery.deliver_pending() == () and not backend.calls


async def test_unwritable_accounting_reaches_finalizer_but_leaves_claim_open(system):
    operation, backend, recovery = await retiring(system)
    async with system[0].acquire() as connection:
        await connection.execute(
            "ALTER TABLE controller_execution_accounting ADD CONSTRAINT refuse_accounting CHECK(false) NOT VALID"
        )
    (result,) = await recovery.run()
    assert result.action == "deferred" and backend.inventory_reads >= 3
    await assert_open(system[0], operation.grant.lease.operation_id)
    assert not backend.calls


async def test_release_disposition_is_retained_when_inventory_disallows_release(system):
    pool, admit, server, _, _, registry, _ = system
    operation, token = await admit("provision")
    for step in ("1", "2", "3"):
        await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": step}}
        )
    finalizer = server._after_step

    async def crash_after_inventory(grant, result):
        await finalizer(grant, result)
        # The complete provision has real running resources, but a killed worker
        # never commits shared terminal settlement after its inventory returned.
        raise asyncio.CancelledError

    server._after_step = crash_after_inventory
    try:
        with pytest.raises(asyncio.CancelledError):
            await server.dispatch(
                {
                    "token": token,
                    "method": "execute_step",
                    "arguments": {"step_id": "4"},
                }
            )
    finally:
        server._after_step = finalizer
    actor = await scope(pool, operation.grant.lease.operation_id)
    await expire(pool, operation.grant.lease.operation_id)
    backend = ObservationBackend(registry, finalizer.provider, operation, actor)
    recovery = ScopedRecovery(
        pools(registry),
        principal=actor,
        observe_claim=backend.observe,
        finalize=RecoveryFinalizer(pools(registry), backend),
        ledger=backend,
    )
    # Exercise the accounting fence with a real verified finalization. Even if a
    # call disposition says RELEASE, the finalizer's release decision is binding.
    settled = recovery._settled

    async def all_release(connection, lease, result):
        assert (
            recovery.prepared[lease.operation_id, lease.fence_token][
                "release_permitted"
            ]
            is False
        )
        altered = SweepResult(
            result.operation_id,
            result.action,
            budget_disposition="release",
            call_dispositions=(("absent-call", BudgetDisposition.RELEASE),),
        )
        await settled(connection, lease, altered)

    recovery._settled = all_release
    await recovery.run()
    assert backend.calls[0]["accounting"]["budget"] == "retain"
    assert backend.calls[0]["accounting"]["release_permitted"] is False


async def test_immutable_receipt_and_exact_original_identity(system):
    operation, backend, recovery = await retiring(system)
    await recovery.run()
    receipt = backend.calls[0]
    assert receipt["operation_id"] == operation.grant.lease.operation_id
    assert receipt["job_id"] == operation.job_id
    assert receipt["org_id"] == operation.grant.lease.org_id
    async with system[0].acquire() as connection:
        for sql in (
            "UPDATE harness_recovery_settlements SET accounting='{}'::jsonb",
            "UPDATE harness_recovery_settlements SET delivered_at=NULL",
            "DELETE FROM harness_recovery_settlements",
        ):
            with pytest.raises(Exception, match="immutable"):
                await connection.execute(sql)


async def test_real_service_pass_needs_no_remaining_execution_grant(system):
    from superplane_executor.service import recover

    operation, backend, _ = await retiring(system)
    system[5].handoffs.clear()
    await recover(backend, pools(system[5]))
    assert backend.calls[0]["operation_id"] == operation.grant.lease.operation_id


async def test_cursor_survives_restart_and_does_not_limit_permanent_history(system):
    pool, admit, server, _, _, registry, _ = system
    operation = await start_provisioning(admit, server)
    actor = await scope(pool, operation.grant.lease.operation_id)
    # Existing journal history (including IDs before the eligible lease) cannot
    # occupy the recovery page. The candidate limit is applied to expired leases.
    async with pool.acquire() as connection:
        for i in range(40):
            await connection.execute(
                "INSERT INTO controller_provider_requests VALUES($1,$2,$3,$4,'old','launch','old-request',NULL)",
                "old-key-" + str(i),
                "000-history-" + str(i),
                actor.org_id,
                actor.workspace_id,
            )
    await expire(pool, operation.grant.lease.operation_id)
    first = ScopedRecovery(pools(registry), principal=actor)
    assert await first._candidates(1) == {operation.grant.lease.operation_id}
    restarted = ScopedRecovery(pools(registry), principal=actor)
    assert await restarted._candidates(1) == frozenset()  # wraps the durable cursor
    assert await restarted._candidates(1) == {operation.grant.lease.operation_id}


async def test_lost_acknowledgement_retries_the_same_immutable_receipt(system):
    operation, backend, recovery = await retiring(system)
    original = backend.deliver_settlement
    attempts = []

    async def lost_reply(**receipt):
        attempts.append(receipt)
        await original(**receipt)
        raise OSError("response lost after receiver accepted")

    backend.deliver_settlement = lost_reply
    with pytest.raises(OSError, match="after receiver accepted"):
        await recovery.run()
    backend.deliver_settlement = original
    assert await recovery.deliver_pending() == (operation.grant.lease.operation_id,)
    assert backend.calls[0] == backend.calls[1] == attempts[0]
    assert await recovery.deliver_pending() == ()


async def test_bounded_cursor_pages_all_eligible_leases_across_restart(system):
    from harness_jobs import OperationStore
    from tests.test_scoped_recovery_postgres import _expired_operation

    pool, _, _, _, _, registry, _ = system
    async with pool.acquire() as connection:
        workspace, domain_org = await connection.fetchrow(
            "SELECT id::text,org_id::text FROM workspaces"
        )
        ids = set()
        for index in range(31):
            record, _ = await _expired_operation(
                OperationStore(),
                connection,
                key="page-" + str(index),
                org=domain_org,
                workspace=workspace,
            )
            ids.add(record.operation_id)
            await connection.execute(
                "INSERT INTO controller_provider_requests VALUES($1,$2,$3,$4,'old','launch','old-request',NULL)",
                "page-key-" + str(index),
                record.operation_id,
                domain_org,
                workspace,
            )
    actor = await scope(pool, next(iter(ids)))
    first = ScopedRecovery(pools(registry), principal=actor)
    page = await first._candidates(25)
    assert len(page) == 25
    restarted = ScopedRecovery(pools(registry), principal=actor)
    remainder = await restarted._candidates(25)
    assert len(remainder) == 6 and page.isdisjoint(remainder)
    assert page | remainder == ids
