"""Real transaction, fencing and execution tests for credential-owner recovery."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from harness_jobs.execution import (
    CallOutcome,
    OperationExecutor,
    ProviderCallRefused,
    read_call,
)
from harness_jobs.execution_plan import (
    PlanProgress,
    admitted_steps,
    confirmed_plan_progress,
    step_key,
)
from harness_jobs.execution_rpc import ExecutionGrant, ExecutionRPCServer
from harness_jobs.identity import OperationRefused
from harness_jobs.leases import acquire
from harness_jobs.provider_retry import recover_uncredentialed
from harness_jobs.schema import downgrade

from .conftest import cancellation_principal, requires_postgres
from .test_admission_postgres import (
    FakeLedger,
    admit,
    approval,
    envelope,
    principal,
    request,
)

pytestmark = requires_postgres


async def source(
    pool, *, partial=False, bootstrap=False, initial=False, max_runtime_seconds=3600
):
    async with pool.acquire() as connection:
        req = request(
            execution_steps=json.dumps(
                [
                    {
                        "step_id": "bootstrap-workspace"
                        if bootstrap
                        else "apply-infrastructure"
                        if partial
                        else "prepare-infrastructure"
                        if initial
                        else "prepare",
                        "provider": "superplane-lifecycle"
                        if partial or bootstrap or initial
                        else "test",
                        "operation_kind": "bootstrap-workspace"
                        if bootstrap
                        else "apply-infrastructure"
                        if partial
                        else "prepare-infrastructure"
                        if initial
                        else "prepare",
                        "target": "a" * 64 if partial or bootstrap else "target",
                    }
                ]
            )
        )
        budget = envelope(max_runtime_seconds=max_runtime_seconds)
        result = await admit(
            connection,
            FakeLedger(),
            req=req,
            record=approval(req=req, approved=budget),
            requested=budget,
        )
        record = result.operation.record
        lease = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="worker",
            attempt_id="attempt-1",
        )
    (step,) = admitted_steps(record)
    executor = OperationExecutor(lease, connect=pool.acquire)
    await executor.record_intent(
        idempotency_key=step_key(record, step),
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )
    async with pool.acquire() as connection:
        await connection.execute("UPDATE harness_operations SET state='running'")
        await connection.execute(
            "UPDATE harness_operation_leases SET "
            "expires_at=clock_timestamp()-interval '1 second',"
            "runtime_deadline=clock_timestamp()-interval '1 second'"
        )
    return record, lease, step


async def authorize(operation, paid):
    assert operation["operation_id"] == paid["operation_id"]
    return datetime.now(UTC) + timedelta(hours=1)


async def proof(operation, lease, call):
    assert call["fence_token"] == lease["fence_token"]
    return {
        "credentials_delivered": False,
        "original_authority_expired": True,
        "evidence_kind": "credential-delivery-barrier-v1",
    }


async def recover(connection, record, **overrides):
    args = dict(
        principal=principal(),
        operation_id=record.operation_id,
        expected_version=record.version,
        reauthorization_id="fresh-human-decision",
        authorize=authorize,
        prove_not_started=proof,
    )
    args.update(overrides)
    return await recover_uncredentialed(connection, **args)


async def test_owner_proven_restart_appends_chain_and_preserves_attempt_budget(pool):
    from harness_jobs.provider_retry import effective_step_key
    from harness_jobs.store import _record

    record, source_lease, step = await source(pool)
    async with pool.acquire() as connection:
        admission = dict(
            await connection.fetchrow("SELECT * FROM harness_approval_consumption")
        )
        first = await recover(connection, record)
        lease = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="retry",
            attempt_id="retry",
        )
        current = _record(await connection.fetchrow("SELECT * FROM harness_operations"))
    executor = OperationExecutor(lease, connect=pool.acquire)
    await executor.record_intent(
        idempotency_key=first["replacement_key"],
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET "
            "expires_at=clock_timestamp()-interval '1 second',"
            "runtime_deadline=clock_timestamp()-interval '1 second'"
        )

        async def start_barrier(operation, expired, call):
            assert expired["fence_token"] == lease.fence_token
            assert call["idempotency_key"] == first["replacement_key"]
            return {
                "evidence_kind": "planning-start-barrier-v1",
                "planning_started": False,
                "original_authority_expired": True,
                "issued_sessions_expired": True,
                "owner_evidence_id": "protected-owner-record",
                "issuance_audit_ids": ["retained-audit"],
            }

        second = await recover(
            connection,
            current,
            reauthorization_id="second-human-decision",
            recovery_scope="unstarted-planning",
            prove_not_started=start_barrier,
        )
        assert second["original_key"] == first["replacement_key"]
        assert (
            dict(
                await connection.fetchrow(
                    "SELECT * FROM harness_provider_retries WHERE source_fence=$1",
                    source_lease.fence_token,
                )
            )
            == first
        )
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_approval_consumption")
            )
            == admission
        )
        assert (
            await recover(connection, record) == first
        )  # Old response replay remains immutable.
        assert (
            await effective_step_key(connection, current, step)
            == second["replacement_key"]
        )
        assert (
            await confirmed_plan_progress(connection, record.operation_id)
            is PlanProgress.PREFIX
        )
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 2
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_call_intent "
                "WHERE stage='reconciled'"
            )
            == 2
        )
        fresh = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="third",
            attempt_id="third",
        )
        assert fresh.attempts == source_lease.attempts + 2
        assert fresh.max_attempts == source_lease.max_attempts
        with pytest.raises(Exception, match="cannot discard planning recovery history"):
            async with connection.transaction():
                await downgrade(connection, target=12)


async def test_original_intent_and_admission_survive_one_successful_retry(pool):
    record, lease, step = await source(pool)
    async with pool.acquire() as connection:
        admission = dict(
            await connection.fetchrow("SELECT * FROM harness_approval_consumption")
        )
        receipt = await recover(connection, record)
        assert await recover(connection, record) == receipt
        original = await read_call(connection, idempotency_key=step_key(record, step))
        assert original.outcome is CallOutcome.FAILED
        assert (
            original.attempt_id == lease.attempt_id
            and original.fence_token == lease.fence_token
        )
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_approval_consumption")
            )
            == admission
        )
        assert (
            await confirmed_plan_progress(connection, record.operation_id)
            is PlanProgress.PREFIX
        )
        latest = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="next",
            attempt_id="next",
        )
        assert (
            latest.attempts == lease.attempts + 1
            and latest.fence_token == lease.fence_token + 2
        )
    calls = []

    async def provider(call):
        calls.append(call)
        return CallOutcome.SUCCEEDED, "real provider test result", "artifact"

    grant = ExecutionGrant(cancellation_principal("next"), latest)
    server = ExecutionRPCServer(
        connect=pool.acquire, provider_call=provider, authenticate=lambda _: None
    )
    runtime = OperationExecutor(latest, connect=pool.acquire, provider_call=provider)
    await server.execute_step(grant, runtime, step.step_id)
    assert len(calls) == 1 and calls[0].idempotency_key == receipt["replacement_key"]
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT state FROM harness_operations")
            == "succeeded"
        )
        assert (
            await confirmed_plan_progress(connection, record.operation_id)
            is PlanProgress.COMPLETE
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_provider_call_intent"
            )
            == 2
        )
        row = await connection.fetchrow("SELECT * FROM harness_operations")
        for key in (
            "operation_id",
            "job_id",
            "attempt_id",
            "plan_digest",
            "request_payload",
        ):
            assert row[key] == getattr(record, key)
        with pytest.raises(OperationRefused):
            await recover(connection, record, reauthorization_id="another-decision")
        with pytest.raises(Exception, match="immutable"):
            async with connection.transaction():
                await connection.execute("DELETE FROM harness_provider_retries")
        with pytest.raises(Exception, match="cannot discard"):
            async with connection.transaction():
                await downgrade(connection, target=11)


@pytest.mark.parametrize(
    "change",
    [
        "live",
        "cancelled",
        "cleanup",
        "terminal",
        "version",
        "foreign",
        "budget",
        "result",
        "proof",
        "expiry",
    ],
)
async def test_recovery_refuses_uncertainty_without_changing_original(pool, change):
    record, lease, step = await source(pool)
    args = {}
    async with pool.acquire() as connection:
        edits = {
            "live": (
                "UPDATE harness_operation_leases SET "
                "runtime_deadline=clock_timestamp()+interval '1 hour'"
            ),
            "cancelled": (
                "UPDATE harness_operations SET cancel_requested_at=clock_timestamp()"
            ),
            "cleanup": "UPDATE harness_operations SET cleanup_required=true",
            "terminal": "UPDATE harness_operations SET state='unknown'",
            "budget": (
                "UPDATE harness_approval_consumption SET reservation_state='retained'"
            ),
            "result": (
                "UPDATE harness_provider_call_intent SET provider_ref='accepted-handle'"
            ),
        }
        if change in edits:
            await connection.execute(edits[change])
        if change == "version":
            args["expected_version"] = record.version + 1
        if change == "foreign":
            args["principal"] = cancellation_principal("another-human")
        if change == "proof":

            async def denied(*args):
                return {
                    "credentials_delivered": True,
                    "original_authority_expired": True,
                    "evidence_kind": "credential-delivery-barrier-v1",
                }

            args["prove_not_started"] = denied
        if change == "expiry":

            async def expired(*args):
                return datetime.now(UTC) + timedelta(minutes=10)

            args["authorize"] = expired
        original = dict(
            await connection.fetchrow("SELECT * FROM harness_provider_call_intent")
        )
        before = dict(
            await connection.fetchrow("SELECT * FROM harness_operation_leases")
        )
        with pytest.raises(OperationRefused):
            await recover(connection, record, **args)
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_provider_call_intent")
            )
            == original
        )
        assert (
            dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
            == before
        )
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 0
        )


async def test_active_provider_lock_refuses_recovery_and_old_worker_stays_fenced(pool):
    record, lease, step = await source(pool)
    async with pool.acquire() as held, pool.acquire() as recovery:
        key = f"harness-provider-dispatch:{record.operation_id}"
        await held.execute("SELECT pg_advisory_lock(hashtextextended($1,0))", key)
        try:
            with pytest.raises(OperationRefused, match="still active"):
                await recover(recovery, record)
        finally:
            await held.execute("SELECT pg_advisory_unlock(hashtextextended($1,0))", key)
        await recover(recovery, record)
    old = OperationExecutor(lease, connect=pool.acquire)
    with pytest.raises(ProviderCallRefused):
        await old.record_intent(
            idempotency_key=step_key(record, step),
            provider=step.provider,
            operation_kind=step.operation_kind,
            target=step.target,
        )


async def test_authority_withdrawn_during_proof_rolls_back_every_write(pool):
    record, _, _ = await source(pool)
    checks = 0

    async def withdraw(operation, paid):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise OperationRefused("decision withdrawn during proof")
        return await authorize(operation, paid)

    async with pool.acquire() as connection:
        before = {
            table: [
                dict(row) for row in await connection.fetch("SELECT * FROM " + table)
            ]
            for table in (
                "harness_operations",
                "harness_operation_leases",
                "harness_provider_call_intent",
                "harness_execution_audit",
            )
        }
        with pytest.raises(OperationRefused, match="withdrawn"):
            await recover(connection, record, authorize=withdraw)
        for table, rows in before.items():
            assert [
                dict(row) for row in await connection.fetch("SELECT * FROM " + table)
            ] == rows
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 0
        )


async def test_concurrent_resume_has_one_receipt_and_one_fence_advance(pool):
    import asyncio

    record, lease, _ = await source(pool)

    async def resume():
        async with pool.acquire() as connection:
            try:
                return await recover(connection, record)
            except OperationRefused as error:
                assert "still active" in str(error)
                return None

    results = await asyncio.gather(resume(), resume())
    assert any(result is not None for result in results)
    async with pool.acquire() as connection:
        receipt = await recover(connection, record)
        assert all(result is None or result == receipt for result in results)
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT fence_token FROM harness_operation_leases"
            )
            == lease.fence_token + 1
        )


@pytest.mark.parametrize("field", ["provider_ref", "target", "provider", "job_id"])
async def test_changed_original_refusal_cannot_be_excluded_from_execution(pool, field):
    record, _, _ = await source(pool)
    async with pool.acquire() as connection:
        await recover(connection, record)
        if field != "provider_ref":
            with pytest.raises(Exception, match="binding is immutable"):
                await connection.execute(
                    "UPDATE harness_provider_call_intent SET "
                    + field
                    + "='changed-source'"
                )
            return
        await connection.execute(
            "UPDATE harness_provider_call_intent SET provider_ref='changed-source'"
        )
        with pytest.raises(OperationRefused, match="evidence changed"):
            await confirmed_plan_progress(connection, record.operation_id)


async def test_explicit_owner_extension_preserves_five_attempts_and_replay(pool):
    record, _, _ = await source(pool)
    async with pool.acquire() as connection:
        await connection.execute("UPDATE harness_operation_leases SET attempts=5")
        original = dict(
            await connection.fetchrow("SELECT * FROM harness_approval_consumption")
        )

        async def extended(operation, lease, call):
            return {
                "evidence_kind": "planning-start-barrier-v1",
                "planning_started": False,
                "original_authority_expired": True,
                "issued_sessions_expired": True,
                "owner_evidence_id": "protected-owner-record",
                "attempt_extension": {
                    "authorized_by": principal().subject,
                    "authorization": "5 more additional attempts are authorized",
                    "previous_limit": 5,
                    "recorded_attempts": 5,
                    "additional_attempts": 5,
                },
            }

        args = dict(recovery_scope="unstarted-planning", prove_not_started=extended)
        receipt = await recover(connection, record, **args)
        assert await recover(connection, record, **args) == receipt
        lease = await connection.fetchrow("SELECT * FROM harness_operation_leases")
        assert (lease["attempts"], lease["max_attempts"]) == (5, 10)
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_approval_consumption")
            )
            == original
        )
        next_lease = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="sixth",
            attempt_id="sixth",
        )
        assert (next_lease.attempts, next_lease.max_attempts) == (6, 10)
        assert await recover(connection, record, **args) == receipt
        assert (
            await connection.fetchval("SELECT attempts FROM harness_operation_leases")
            == 6
        )
        with pytest.raises(Exception, match="immutable"):
            await connection.execute("UPDATE harness_provider_retries SET proof='{}'")


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "foreign-human",
        "stale-count",
        "stale-limit",
        "too-many",
        "boolean",
        "empty-authorization",
        "wrong-scope",
    ],
)
async def test_attempt_extension_refuses_unbound_or_unapproved_changes(pool, change):
    record, _, _ = await source(pool)
    async with pool.acquire() as connection:
        await connection.execute("UPDATE harness_operation_leases SET attempts=5")
        before = dict(
            await connection.fetchrow("SELECT * FROM harness_operation_leases")
        )

        async def extended(operation, lease, call):
            ext = dict(
                authorized_by=principal().subject,
                authorization="Five additional attempts authorized",
                previous_limit=5,
                recorded_attempts=5,
                additional_attempts=5,
            )
            mutations = {
                "foreign-human": ("authorized_by", "someone-else"),
                "stale-count": ("recorded_attempts", 4),
                "stale-limit": ("previous_limit", 4),
                "too-many": ("additional_attempts", 6),
                "boolean": ("additional_attempts", True),
                "empty-authorization": ("authorization", ""),
            }
            if change in mutations:
                k, v = mutations[change]
                ext[k] = v
            result = dict(
                evidence_kind="planning-start-barrier-v1",
                planning_started=False,
                original_authority_expired=True,
                issued_sessions_expired=True,
                owner_evidence_id="protected-owner-record",
            )
            if change != "missing":
                result["attempt_extension"] = ext
            if change == "wrong-scope":
                result.update(
                    evidence_kind="credential-delivery-barrier-v1",
                    credentials_delivered=False,
                )
            return result

        with pytest.raises(OperationRefused):
            await recover(
                connection,
                record,
                recovery_scope="uncredentialed-planning"
                if change == "wrong-scope"
                else "unstarted-planning",
                prove_not_started=extended,
            )
        assert (
            dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
            == before
        )
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 0
        )


async def completion_proof(operation, lease, call):
    return {
        "evidence_kind": "saved-plan-finalization-v1",
        "planning_started": True,
        "apply_started": False,
        "original_authority_expired": True,
        "issued_sessions_expired": True,
        "owner_evidence_id": "protected-saved-plan",
        "preparation_boundary": {
            "kind": "saved-plan-finalization-v1",
            "observation": {
                "offline_guard_passed": True,
                "files": {
                    name: f"{index:064x}"
                    for index, name in enumerate(
                        [
                            "workspace.tfplan",
                            "workspace-plan.json",
                            "workspace-inventory.json",
                            "workspace-estimate.json",
                            "workspace-authorization.proposed.json",
                        ],
                        1,
                    )
                },
                "target": {"account_id": "123456789012", "aws_region": "us-west-2"},
                "terraform_version": "1.9.8",
            },
        },
    }


async def test_saved_plan_completion_preserves_final_attempt_and_rejects_live_repeat(
    pool,
):
    record, source_lease, _ = await source(pool)
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET attempts=9,max_attempts=10"
        )
        original = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
        args = dict(
            recovery_scope="saved-plan-completion", prove_not_started=completion_proof
        )
        receipt = await recover(connection, record, **args)
        assert await recover(connection, record, **args) == receipt
        next_lease = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="completion",
            attempt_id="completion",
        )
        assert (next_lease.attempts, next_lease.max_attempts) == (10, 10)
        assert next_lease.fence_token == source_lease.fence_token + 2
        current = dict(await connection.fetchrow("SELECT * FROM harness_operations"))
        for field in (
            "request_payload",
            "plan_digest",
            "job_id",
            "attempt_id",
            "operation_id",
        ):
            assert current[field] == original[field]
        with pytest.raises(OperationRefused, match="not eligible"):
            await recover(
                connection,
                record,
                reauthorization_id="another",
                expected_version=record.version + 1,
                **args,
            )
        with pytest.raises(Exception, match="cannot discard saved plan"):
            async with connection.transaction():
                await downgrade(connection, target=13)


@pytest.mark.parametrize(
    "change", ["scope", "guard", "started", "applied", "sessions", "extension"]
)
async def test_saved_completion_cannot_use_an_unstarted_proof_or_extend_attempts(
    pool, change
):
    record, _, _ = await source(pool)

    async def invalid(*args):
        value = await completion_proof(*args)
        if change == "scope":
            value["evidence_kind"] = "planning-start-barrier-v1"
        elif change == "guard":
            value["preparation_boundary"]["observation"]["offline_guard_passed"] = False
        elif change == "started":
            value["planning_started"] = False
        elif change == "applied":
            value["apply_started"] = True
        elif change == "sessions":
            value["issued_sessions_expired"] = False
        else:
            value["attempt_extension"] = {"additional_attempts": 1}
        return value

    async with pool.acquire() as connection:
        before = dict(
            await connection.fetchrow("SELECT * FROM harness_operation_leases")
        )
        with pytest.raises(OperationRefused):
            await recover(
                connection,
                record,
                recovery_scope="saved-plan-completion",
                prove_not_started=invalid,
            )
        assert (
            dict(await connection.fetchrow("SELECT * FROM harness_operation_leases"))
            == before
        )
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_provider_retries")
            == 0
        )


@pytest.mark.parametrize(
    "change",
    [
        None,
        "plan",
        "json",
        "inventory",
        "estimate",
        "authorization",
        "target",
        "terraform",
        "owner",
        "missing",
        "ceiling",
    ],
)
async def test_interrupted_completion_keeps_plan_identity_and_attempt_capacity(
    pool, change
):
    from harness_jobs.provider_retry import effective_step_key
    from harness_jobs.store import _record

    record, source_lease, step = await source(pool)
    async with pool.acquire() as connection:
        first = await recover(
            connection,
            record,
            recovery_scope="saved-plan-completion",
            prove_not_started=completion_proof,
        )
        lease = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="completion",
            attempt_id="completion",
        )
    executor = OperationExecutor(lease, connect=pool.acquire)
    await executor.record_intent(
        idempotency_key=first["replacement_key"],
        provider=step.provider,
        operation_kind=step.operation_kind,
        target=step.target,
    )
    async with pool.acquire() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET "
            "expires_at=clock_timestamp()-interval '1 second',"
            "runtime_deadline=clock_timestamp()-interval '1 second'"
        )
        if change == "ceiling":
            await connection.execute(
                "UPDATE harness_operation_leases SET attempts=max_attempts"
            )
        before = dict(
            await connection.fetchrow("SELECT * FROM harness_operation_leases")
        )

        async def fresh_proof(*args):
            value = await completion_proof(*args)
            value["owner_evidence_id"] = "fresh-protected-inspection"
            observation = value["preparation_boundary"]["observation"]
            names = {
                "plan": "workspace.tfplan",
                "json": "workspace-plan.json",
                "inventory": "workspace-inventory.json",
                "estimate": "workspace-estimate.json",
                "authorization": "workspace-authorization.proposed.json",
            }
            if change in names:
                observation["files"][names[change]] = "f" * 64
            elif change == "target":
                observation["target"]["aws_region"] = "us-east-1"
            elif change == "terraform":
                observation["terraform_version"] = "another"
            elif change == "owner":
                value["owner_evidence_id"] = "protected-saved-plan"
            elif change == "missing":
                del observation["files"]["workspace.tfplan"]
            return value

        kwargs = dict(
            reauthorization_id="fresh-after-interruption",
            expected_version=first["restored_version"],
            recovery_scope="saved-plan-completion",
            prove_not_started=fresh_proof,
        )
        if change:
            with pytest.raises(OperationRefused):
                await recover(connection, record, **kwargs)
            assert (
                dict(
                    await connection.fetchrow("SELECT * FROM harness_operation_leases")
                )
                == before
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_retries"
                )
                == 1
            )
            assert (
                await connection.fetchval(
                    "SELECT stage FROM harness_provider_call_intent "
                    "WHERE idempotency_key=$1",
                    first["replacement_key"],
                )
                == "intended"
            )
            return
        second = await recover(connection, record, **kwargs)
        assert await recover(connection, record, **kwargs) == second
        assert second["original_key"] == first["replacement_key"]
        assert second["source_fence"] == lease.fence_token
        current = _record(await connection.fetchrow("SELECT * FROM harness_operations"))
        assert (
            await effective_step_key(connection, current, step)
            == second["replacement_key"]
        )
        assert (
            current.request_payload == record.request_payload
            and current.plan_digest == record.plan_digest
        )
        restored = await connection.fetchrow("SELECT * FROM harness_operation_leases")
        assert (restored["attempts"], restored["max_attempts"]) == (
            before["attempts"],
            before["max_attempts"],
        )
        next_lease = await acquire(
            connection,
            operation_id=record.operation_id,
            holder="finalization",
            attempt_id="finalization",
        )
        assert next_lease.attempts == lease.attempts + 1
        assert next_lease.max_attempts == lease.max_attempts


@pytest.mark.parametrize(
    "changed", [None, "apply", "unexpired", "extension", "wrong-phase"]
)
async def test_interrupted_initial_plan_keeps_admission_and_records_distinct_retry(
    pool, changed
):
    record, old_lease, step = await source(pool, initial=changed != "wrong-phase")

    async def interrupted(operation, lease, call):
        boundary = {
            "kind": "interrupted-read-only-plan-v1",
            "read_only_recipe": True,
            "backend_observation": {
                "versions_absent": True,
                "delete_markers_absent": True,
                "checksum_absent": True,
            },
        }
        owner = {
            "record_id": "owner",
            "evidence_kind": boundary["kind"],
            "planning_started": True,
            "apply_started": False,
            "preparation_boundary": boundary,
        }
        value = {
            "evidence_kind": boundary["kind"],
            "planning_started": True,
            "apply_started": False,
            "original_authority_expired": True,
            "issued_sessions_expired": True,
            "owner_evidence_id": "owner",
            "owner_evidence": owner,
            "preparation_boundary": boundary,
        }
        if changed == "apply":
            value["apply_started"] = True
        if changed == "unexpired":
            value["original_authority_expired"] = False
        if changed == "extension":
            value["attempt_extension"] = {"additional_attempts": 1}
        return value

    async with pool.acquire() as connection:
        paid = dict(
            await connection.fetchrow("SELECT * FROM harness_approval_consumption")
        )
        original = dict(
            await connection.fetchrow("SELECT * FROM harness_provider_call_intent")
        )
        args = dict(
            recovery_scope="interrupted-planning", prove_not_started=interrupted
        )
        if changed:
            with pytest.raises(OperationRefused):
                await recover(connection, record, **args)
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM harness_provider_retries"
                )
                == 0
            )
            assert (
                dict(
                    await connection.fetchrow(
                        "SELECT * FROM harness_provider_call_intent"
                    )
                )
                == original
            )
        else:
            receipt = await recover(connection, record, **args)
            assert receipt["recovery_scope"] == "interrupted-planning"
            assert receipt["original_key"] == original["idempotency_key"]
            assert receipt["replacement_key"] != receipt["original_key"]
            assert json.loads(receipt["source_intent"]) == json.loads(
                json.dumps(original, default=str)
            )
            lease = await connection.fetchrow("SELECT * FROM harness_operation_leases")
            assert lease["fence_token"] == old_lease.fence_token + 1
            assert lease["attempts"] == old_lease.attempts
            assert lease["max_attempts"] == old_lease.max_attempts
            with pytest.raises(Exception, match="cannot discard interrupted"):
                await downgrade(connection, target=17)
        assert (
            dict(
                await connection.fetchrow("SELECT * FROM harness_approval_consumption")
            )
            == paid
        )
