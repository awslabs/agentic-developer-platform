"""Two native jobs cannot claim the same retained release across processes."""

import asyncio
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from src.orchestration import execution_store
from src.orchestration import operations_release_job as jobs
from src.orchestration import operations_shared_release as releases
from src.orchestration.deployment_manifest import PhysicalTarget, TargetEvidence
from src.orchestration.environment_leases import LeaseHolder, acquire_lease, foundation_release_scope, shared_release_held
from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus, OutcomeKind, PhaseAdvance
from src.orchestration.execution_store import advance_execution
from src.orchestration.models import (
    OrchestrationAction,
    OrchestrationEnvironmentLease,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.base import Base
from tests.orchestration.test_operations_release_job import job  # noqa: F401
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, store  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def retained_job(cycle, job, monkeypatch):  # noqa: F811
    ctx = cycle
    monkeypatch.setattr(jobs, "accepted_release_pins", releases.accepted_release_pins)
    monkeypatch.setattr(jobs, "_live_authority_conflict", execution_store._live_authority_conflict)
    ctx.claim.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    job.entry.connection_id = "delivery-connection"
    job.contract.environment_connection_ids = ["delivery-connection"]
    job.report.dispatch_metadata["orchestration"]["release_binding"]["connection_id"] = "delivery-connection"
    job.action.detail["correlation"] = ctx.root
    job.action.detail["repo"] = ctx.binding.repo
    job.action.detail["provider_repository_id"] = ctx.binding.provider_repository_id
    job.contract.node.operational_release_binding.repository = ctx.binding.repo
    job.claims.update(
        repository=ctx.binding.repo,
        repository_id=str(ctx.binding.provider_repository_id),
        workflow_ref=f"{ctx.binding.repo}/.github/workflows/{job.body.workflow}@refs/heads/main",
    )
    job.run["repository"] = {"id": ctx.binding.provider_repository_id}
    job.run["display_title"] = "ADP deployment " + ctx.root
    role = "arn:aws:iam::123456789012:role/adp-dev-trusted-deployment"
    body = job.body.model_copy(update={"correlation": ctx.root, "role_arn": "arn:aws:iam::123456789012:role/test", "mutation_role_arn": role})
    from src.orchestration.deployment_manifest import PhysicalTarget, TargetEvidence

    raw = job.action.detail["shared_release"]["target"]
    physical = PhysicalTarget(
        **{**raw, "account_id": "123456789012", "evidence": TargetEvidence("registered-aws-role:delivery-connection", raw["evidence"]["verified_at"])}
    )
    job.entry.mutation_role_arn = role
    job.report.dispatch_metadata["orchestration"]["release_binding"]["mutation_role_arn"] = role
    job.action.detail["shared_release"].update(target=asdict(physical), mutation_role_arn=role)
    shared, owner = foundation_release_scope(
        node=ctx.node,
        flow=ctx.flow,
        entry=job.entry,
        component="gateway-backend",
        target=physical,
        environment="dev",
        source_revision=body.source_revision,
    )
    job.action.detail["shared_release"].update(owner=owner, target_key=shared.canonical_key, generation=ctx.identity.claim_generation)
    monkeypatch.setattr(jobs, "shared_release_held", shared_release_held)
    async with ctx.factory() as session:
        connection = await session.connection()
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[OrchestrationEnvironmentLease.__table__]))
        claim = await session.get(OrchestrationWorkClaim, ctx.identity.claim_id)
        claim.lease_expires_at = ctx.claim.lease_expires_at
        row = await session.get(OrchestrationExecution, ctx.execution.id)
        row.handoff_receipt_ref = None
        row.status = "runnable"
        job.contract.node.operational_release_binding.default_branch = "main"
        job.contract.node.operational_release_binding.predecessors = [SimpleNamespace(address="reviewed-build")]
        metadata = job.report.dispatch_metadata["orchestration"]
        metadata["deployment_revisions"] = dict(job.pins.return_value)
        metadata["release_binding"].update(
            pins=dict(job.pins.return_value),
            repository=ctx.binding.repo,
            component="gateway-backend",
            accepted_plan_version=row.accepted_plan_version,
            receipts=[{"address": "reviewed-build"}],
        )
        session.add(
            OrchestrationAction(
                org_id=ctx.identity.org_id,
                execution_id=row.id,
                operation_key="operations-release:1",
                kind="operations_release_binding",
                status="succeeded",
                attempt=row.cycle,
                detail=deepcopy(metadata["release_binding"]),
            )
        )
        session.add(
            OrchestrationRunReport(
                run_id=ctx.root,
                credential_hash="f" * 64,
                org_id=ctx.identity.org_id,
                flow_id=ctx.flow.id,
                node_id=ctx.node.id,
                attempt=ctx.node.attempts,
                persona="operations",
                repo=ctx.binding.repo,
                installation_id=ctx.binding.installation_id,
                provider_repository_id=ctx.binding.provider_repository_id,
                dispatch_metadata=job.report.dispatch_metadata,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        session.add(
            OrchestrationAction(
                id="native-release-job",
                org_id=ctx.identity.org_id,
                execution_id=row.id,
                operation_key="native-release-test",
                kind="operations_native_release_dispatch",
                status="prepared",
                attempt=1,
                detail=job.action.detail,
            )
        )
        await session.flush()
        outcome = await acquire_lease(
            session,
            target=shared,
            holder=LeaseHolder(ctx.identity.org_id, owner, ctx.identity.claim_generation),
            manifest_entry_id=job.entry.entry_id,
            release_ref=body.source_revision,
        )
        assert outcome.applied
        await session.commit()
    job.run["created_at"] = datetime.now(UTC).isoformat()

    async def resolve(session, *, authorize_scope, **kwargs):
        authorize_scope("delivery-connection", "arn:aws:iam::123456789012:role/test")
        return SimpleNamespace(physical=physical, role_arn=body.role_arn)

    job.targets.resolve.side_effect = resolve

    guard = jobs.NativeReleaseJob(
        ctx.factory,
        provider=job.provider,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: job.entry),
        authority=ctx.service,
        targets=job.targets,
    )
    return ctx, job, body, guard


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_retained_native_job_pin_survives_new_service_and_refuses_second_run(retained_job):
    ctx, prepared, body, guard = retained_job
    await guard.authorize(body, prepared.claims)
    restarted = jobs.NativeReleaseJob(
        ctx.factory,
        provider=prepared.provider,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        authority=ctx.service,
        targets=prepared.targets,
    )
    await restarted.authorize(body, prepared.claims)
    other = {**prepared.claims, "run_id": "43"}
    prepared.run["id"] = 43
    with pytest.raises(HTTPException) as error:
        await restarted.authorize(body, other)
    assert error.value.status_code == 403
    async with ctx.factory() as session:
        action = await session.get(OrchestrationAction, "native-release-job")
        assert action.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_competing_first_attempts_admit_only_one_native_run(retained_job):
    ctx, prepared, body, guard = retained_job
    reached = asyncio.Event()
    arrivals = []
    original = prepared.provider.request.side_effect

    async def request(binding, method, path, **kwargs):
        if "/actions/runs/" not in path:
            return await original(binding, method, path, **kwargs)
        arrivals.append(path)
        if len(arrivals) == 2:
            reached.set()
        await asyncio.wait_for(reached.wait(), timeout=5)
        return SimpleNamespace(json=lambda: {**prepared.run, "id": int(path.rsplit("/", 1)[-1])})

    prepared.provider.request.side_effect = request
    other = {**prepared.claims, "run_id": "43"}
    first, second = await asyncio.wait_for(
        asyncio.gather(guard.authorize(body, prepared.claims), guard.authorize(body, other), return_exceptions=True), timeout=15
    )
    assert len(arrivals) == 2
    assert sum(outcome is None for outcome in (first, second)) == 1
    assert sum(isinstance(outcome, HTTPException) and outcome.status_code == 403 for outcome in (first, second)) == 1
    async with ctx.factory() as session:
        action = await session.get(OrchestrationAction, "native-release-job")
        assert action.detail["release_job"]["run_id"] in {"42", "43"}
        assert action.detail["release_job"]["run_attempt"] == "1"


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
@pytest.mark.parametrize("displacement", ["claim", "lease"])
async def test_pinned_native_job_refuses_owner_loss_across_restart(retained_job, displacement):
    ctx, prepared, body, guard = retained_job
    await guard.authorize(body, prepared.claims)
    async with ctx.factory() as session:
        if displacement == "claim":
            claim = await session.get(OrchestrationWorkClaim, ctx.identity.claim_id)
            claim.generation += 1
        else:
            lease = await session.scalar(
                select(OrchestrationEnvironmentLease).where(
                    OrchestrationEnvironmentLease.canonical_target_key == prepared.action.detail["shared_release"]["target_key"]
                )
            )
            assert lease is not None
            lease.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()
    restarted = jobs.NativeReleaseJob(
        ctx.factory,
        provider=prepared.provider,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        authority=ctx.service,
        targets=prepared.targets,
    )
    with pytest.raises(HTTPException) as error:
        await restarted.authorize(body, prepared.claims)
    assert error.value.status_code == 403


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_native_guard_and_execution_advance_share_lock_order(retained_job, monkeypatch):
    ctx, prepared, body, guard = retained_job
    store_locked = asyncio.Event()
    guard_waiting = asyncio.Event()
    resume_store = asyncio.Event()
    original = execution_store._live_authority_conflict

    async def pause_after_authority_locks(*args, **kwargs):
        outcome = await original(*args, **kwargs)
        store_locked.set()
        await asyncio.wait_for(resume_store.wait(), timeout=5)
        return outcome

    monkeypatch.setattr(execution_store, "_live_authority_conflict", pause_after_authority_locks)

    async def guard_authority(*args, **kwargs):
        guard_waiting.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(jobs, "_live_authority_conflict", guard_authority)

    async def advance_store():
        async with ctx.factory() as session:
            row = await session.get(OrchestrationExecution, ctx.execution.id)
            outcome = await advance_execution(
                session,
                identity=ctx.identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase(row.phase),
                    status=ExecutionStatus(row.status),
                    expected_revision=row.revision,
                    next_check_at=datetime.now(UTC),
                ),
                pending_action_key=row.pending_action_key,
            )
            await session.commit()
            return outcome

    store_task = asyncio.create_task(advance_store())
    try:
        await asyncio.wait_for(store_locked.wait(), timeout=5)
        guard_task = asyncio.create_task(guard.authorize(body, prepared.claims))
        await asyncio.wait_for(guard_waiting.wait(), timeout=5)
    finally:
        resume_store.set()
    admission, advanced = await asyncio.wait_for(asyncio.gather(guard_task, store_task, return_exceptions=True), timeout=15)
    assert admission is None
    assert advanced.kind is OutcomeKind.APPLIED
    async with ctx.factory() as session:
        action = await session.get(OrchestrationAction, "native-release-job")
        assert action.detail.get("release_job") == {"run_id": "42", "run_attempt": "1"}


@pytest.fixture
async def native_reservation(retained_job):
    ctx, prepared, body, _ = retained_job
    prepared.entry.workflow.allowed_inputs = {"environment": ["dev"]}
    async with ctx.factory() as session:
        row = await session.get(OrchestrationExecution, ctx.execution.id)
        revision = row.revision
    report = SimpleNamespace(
        run_id=ctx.root,
        repo=ctx.binding.repo,
        provider_repository_id=ctx.binding.provider_repository_id,
        dispatch_metadata={
            **prepared.report.dispatch_metadata,
            "handoff_expect": {
                "execution_id": ctx.execution.id,
                "accepted_plan_version": ctx.identity.accepted_plan_version,
                "claim_id": ctx.identity.claim_id,
                "claim_generation": ctx.identity.claim_generation,
            },
        },
    )
    state = SimpleNamespace(
        node=ctx.node,
        row=row,
        report=report,
        contract=prepared.contract,
        binding=ctx.binding,
        execution={
            "source_revision": {"S": body.source_revision},
            "definition_revision": {"S": prepared.entry.workflow.definition_revision},
            "invocation_id": {"S": ctx.root},
            "default_branch": {"S": "main"},
        },
        values=(
            None,
            None,
            SimpleNamespace(policy=ctx.policy),
            "human",
            None,
            replace(prepared.auth, policy=ctx.policy, member_org_id=ctx.identity.org_id),
        ),
    )
    context = SimpleNamespace(identity=ctx.identity, execution=SimpleNamespace(id=ctx.execution.id, revision=revision))
    raw_target = prepared.action.detail["shared_release"]["target"]
    target = SimpleNamespace(physical=PhysicalTarget(**{**raw_target, "evidence": TargetEvidence(**raw_target["evidence"])}), role_arn=body.role_arn)

    async def resolve(session, *, authorize_scope, **kwargs):
        authorize_scope("delivery-connection", body.role_arn)
        return target

    return ctx, prepared, body, context, state, SimpleNamespace(resolve=resolve)


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_native_reservation_and_execution_advance_share_lock_order(native_reservation, monkeypatch):
    ctx, prepared, body, context, state, resolver = native_reservation
    store_locked = asyncio.Event()
    reservation_waiting = asyncio.Event()
    resume_store = asyncio.Event()
    original_store = execution_store._live_authority_conflict
    original_reservation = releases._live_authority_conflict

    async def pause_store(*args, **kwargs):
        await args[0].execute(select(OrchestrationFlow.id).where(OrchestrationFlow.id == kwargs["flow_id"]).with_for_update())
        store_locked.set()
        await asyncio.wait_for(resume_store.wait(), timeout=5)
        return await original_store(*args, **kwargs)

    async def observe_reservation(*args, **kwargs):
        reservation_waiting.set()
        return await original_reservation(*args, **kwargs)

    monkeypatch.setattr(execution_store, "_live_authority_conflict", pause_store)
    monkeypatch.setattr(releases, "_live_authority_conflict", observe_reservation)

    async def advance_store():
        async with ctx.factory() as session:
            row = await session.get(OrchestrationExecution, ctx.execution.id)
            outcome = await advance_execution(
                session,
                identity=ctx.identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase(row.phase),
                    status=ExecutionStatus(row.status),
                    expected_revision=row.revision,
                    next_check_at=datetime.now(UTC),
                ),
                pending_action_key=row.pending_action_key,
            )
            await session.commit()
            return outcome

    async def reserve():
        async with ctx.factory() as session:
            result = await releases.reserve_native_release(
                session,
                context=context,
                state=state,
                workflow=body.workflow,
                environment="dev",
                initial=True,
                dispatch_revision=body.dispatch_revision,
                resolver=resolver,
                manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
            )
            await session.commit()
            return result

    store_task = asyncio.create_task(advance_store())
    try:
        await asyncio.wait_for(store_locked.wait(), timeout=5)
        reservation_task = asyncio.create_task(reserve())
        await asyncio.wait_for(reservation_waiting.wait(), timeout=5)
    finally:
        resume_store.set()
    reserved, advanced = await asyncio.wait_for(asyncio.gather(reservation_task, store_task, return_exceptions=True), timeout=15)
    assert advanced.kind is OutcomeKind.APPLIED
    assert isinstance(reserved, Exception)
    assert "operations_shared_release_owner_changed" in str(reserved)


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_native_initial_reservation_persists_intent_and_excludes_replay(native_reservation):
    ctx, prepared, body, context, state, resolver = native_reservation
    async with ctx.factory() as session:
        result = await releases.reserve_native_release(
            session,
            context=context,
            state=state,
            workflow=body.workflow,
            environment="dev",
            initial=True,
            dispatch_revision=body.dispatch_revision,
            resolver=resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
        assert result.applied
        await session.commit()
    async with ctx.factory() as session:
        action = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.operation_key.like("native-release:%")))
        assert action is not None
        assert action.detail["shared_release"]["owner"] == prepared.action.detail["shared_release"]["owner"]
    async with ctx.factory() as session:
        with pytest.raises(Exception, match="operations_shared_release_intent_changed"):
            await releases.reserve_native_release(
                session,
                context=context,
                state=state,
                workflow=body.workflow,
                environment="dev",
                initial=True,
                dispatch_revision=body.dispatch_revision,
                resolver=resolver,
                manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
            )


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_competing_native_reservations_persist_one_dispatch_intent(native_reservation):
    ctx, prepared, body, context, state, resolver = native_reservation
    reached = asyncio.Event()
    arrivals = 0
    original_resolve = resolver.resolve

    async def resolve(*args, **kwargs):
        nonlocal arrivals
        target = await original_resolve(*args, **kwargs)
        arrivals += 1
        if arrivals == 2:
            reached.set()
        await asyncio.wait_for(reached.wait(), timeout=5)
        return target

    resolver.resolve = resolve

    async def reserve():
        async with ctx.factory() as session:
            outcome = await releases.reserve_native_release(
                session,
                context=context,
                state=state,
                workflow=body.workflow,
                environment="dev",
                initial=True,
                dispatch_revision=body.dispatch_revision,
                resolver=resolver,
                manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
            )
            await session.commit()
            return outcome

    first, second = await asyncio.wait_for(asyncio.gather(reserve(), reserve(), return_exceptions=True), timeout=15)
    assert arrivals == 2
    assert sum(getattr(outcome, "applied", False) for outcome in (first, second)) == 1
    assert sum(isinstance(outcome, CycleBlockedError) for outcome in (first, second)) == 1
    async with ctx.factory() as session:
        actions = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.operation_key.like("native-release:%")))).all()
        assert len(actions) == 1


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
async def test_native_reservation_refuses_lost_claim_after_target_read(native_reservation):
    ctx, prepared, body, context, state, resolver = native_reservation
    original_resolve = resolver.resolve

    async def displace(*args, **kwargs):
        target = await original_resolve(*args, **kwargs)
        async with ctx.factory() as session:
            claim = await session.get(OrchestrationWorkClaim, ctx.identity.claim_id)
            claim.generation += 1
            await session.commit()
        return target

    resolver.resolve = displace
    async with ctx.factory() as session:
        with pytest.raises(CycleBlockedError, match="operations_shared_release_owner_changed"):
            await releases.reserve_native_release(
                session,
                context=context,
                state=state,
                workflow=body.workflow,
                environment="dev",
                initial=True,
                dispatch_revision=body.dispatch_revision,
                resolver=resolver,
                manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
            )
    async with ctx.factory() as session:
        actions = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.operation_key.like("native-release:%")))).all()
        assert not actions


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
@pytest.mark.parametrize("descendant", [False, True])
async def test_native_initial_reservation_is_accepted_by_independent_job_guard(native_reservation, descendant):
    ctx, prepared, body, context, state, resolver = native_reservation
    async with ctx.factory() as session:
        if descendant:
            prepared.entry.artifact_revision = "d" * 40
            prepared.entry.workflow.definition_revision = "e" * 40
            metadata = state.report.dispatch_metadata["orchestration"]
            metadata["release_binding"].update(manifest_artifact_revision="d" * 40, manifest_definition_revision="e" * 40)
            ledger = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.operation_key == "operations-release:1"))
            ledger.detail = deepcopy(metadata["release_binding"])
            report = await session.get(OrchestrationRunReport, ctx.root)
            report.dispatch_metadata = deepcopy(state.report.dispatch_metadata)
        prior_action = await session.get(OrchestrationAction, "native-release-job")
        prior_lease = await session.scalar(
            select(OrchestrationEnvironmentLease).where(
                OrchestrationEnvironmentLease.canonical_target_key == prepared.action.detail["shared_release"]["target_key"]
            )
        )
        await session.delete(prior_action)
        await session.delete(prior_lease)
        await session.commit()
    async with ctx.factory() as session:
        reserved = await releases.reserve_native_release(
            session,
            context=context,
            state=state,
            workflow=body.workflow,
            environment="dev",
            initial=True,
            dispatch_revision=body.dispatch_revision,
            resolver=resolver,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        )
        assert reserved.applied
        await session.commit()
    async with ctx.factory() as session:
        actions = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_native_release_dispatch"))).all()
        assert len(actions) == 1
        action = actions[0]
        lease = await session.scalar(
            select(OrchestrationEnvironmentLease).where(
                OrchestrationEnvironmentLease.canonical_target_key == action.detail["shared_release"]["target_key"]
            )
        )
        assert lease is not None and lease.owner_action_id == action.detail["shared_release"]["owner"]
    prepared.run["created_at"] = datetime.now(UTC).isoformat()
    guard = jobs.NativeReleaseJob(
        ctx.factory,
        provider=prepared.provider,
        manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
        authority=ctx.service,
        targets=prepared.targets,
    )
    await guard.authorize(body, prepared.claims)
    async with ctx.factory() as session:
        pinned = await session.get(OrchestrationAction, action.id)
        assert pinned.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
@pytest.mark.parametrize("requests", [1, 2])
async def test_admitted_external_wait_reserves_first_native_dispatch_once(native_reservation, requests):
    """The engine's persisted admitted wait is the real first-dispatch state.

    `advance_execution` is used rather than a hand-written row so the state under
    test is the one the engine actually commits before the first actions_dispatch:
    phase ADMITTED with status AWAITING_EXTERNAL and no pending action key. One or
    two concurrent first reservations must still persist exactly one dispatch
    intent, and a replay afterwards must be refused.
    """
    ctx, prepared, body, context, state, resolver = native_reservation
    async with ctx.factory() as session:
        row = await session.get(OrchestrationExecution, ctx.execution.id)
        advanced = await advance_execution(
            session,
            identity=ctx.identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.ADMITTED,
                status=ExecutionStatus.AWAITING_EXTERNAL,
                expected_revision=row.revision,
                next_check_at=datetime.now(UTC),
            ),
        )
        assert advanced.kind is OutcomeKind.APPLIED
        await session.commit()
    async with ctx.factory() as session:
        row = await session.get(OrchestrationExecution, ctx.execution.id)
        assert (row.phase, row.status, row.pending_action_key) == (
            ExecutionPhase.ADMITTED.value,
            ExecutionStatus.AWAITING_EXTERNAL.value,
            None,
        )
        state.row = row
        context.execution = SimpleNamespace(id=row.id, revision=row.revision)

    reached = asyncio.Event()
    arrivals = 0
    original_resolve = resolver.resolve

    async def resolve(*args, **kwargs):
        nonlocal arrivals
        target = await original_resolve(*args, **kwargs)
        arrivals += 1
        if arrivals == requests:
            reached.set()
        await asyncio.wait_for(reached.wait(), timeout=5)
        return target

    resolver.resolve = resolve

    async def reserve():
        async with ctx.factory() as session:
            outcome = await releases.reserve_native_release(
                session,
                context=context,
                state=state,
                workflow=body.workflow,
                environment="dev",
                initial=True,
                dispatch_revision=body.dispatch_revision,
                resolver=resolver,
                manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
            )
            await session.commit()
            return outcome

    outcomes = await asyncio.wait_for(asyncio.gather(*(reserve() for _ in range(requests)), return_exceptions=True), timeout=20)
    assert arrivals == requests
    assert sum(getattr(outcome, "applied", False) for outcome in outcomes) == 1
    assert sum(isinstance(outcome, CycleBlockedError) for outcome in outcomes) == requests - 1
    async with ctx.factory() as session:
        actions = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.operation_key.like("native-release:%")))).all()
        assert len(actions) == 1
        assert actions[0].detail["dispatch_started"] is True
        assert actions[0].detail["shared_release"]["owner"] == prepared.action.detail["shared_release"]["owner"]
    async with ctx.factory() as session:
        with pytest.raises(CycleBlockedError, match="operations_shared_release_intent_changed"):
            await releases.reserve_native_release(
                session,
                context=context,
                state=state,
                workflow=body.workflow,
                environment="dev",
                initial=True,
                dispatch_revision=body.dispatch_revision,
                resolver=resolver,
                manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
            )


@pytest.mark.parametrize("cycle", [{"delivery": True, "missing_handoff": True}], indirect=True)
@pytest.mark.parametrize("native", ["guard", "reservation"])
async def test_native_paths_wait_behind_observer_node_lock_without_deadlock(native_reservation, monkeypatch, native):
    """Neither native path may invert the result observer's node -> execution order.

    The observer in `results.py` takes the node row FOR UPDATE and only then
    advances the execution, so a native guard or reservation that locks the
    execution first deadlocks against it on PostgreSQL. Session A holds the node
    exactly as that observer does; the named native task is observed as soon as it
    asks for the same node FOR UPDATE, A then runs the real `advance_execution`
    with an unchanged phase/status and the current revision, and both sessions must
    settle on their own. The earlier store-only concurrency tests paused inside the
    authority read and so never exercised this outer node lock.
    """
    ctx, prepared, body, context, state, resolver = native_reservation
    requesting = asyncio.Event()
    original_get = AsyncSession.get

    async def instrumented_get(self, entity, ident=None, *args, **kwargs):
        task = asyncio.current_task()
        named = task is not None and task.get_name() == "native-under-test"
        if named and entity is OrchestrationNode and kwargs.get("with_for_update"):
            requesting.set()
        return await original_get(self, entity, ident, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", instrumented_get)

    async def run_guard():
        guard = jobs.NativeReleaseJob(
            ctx.factory,
            provider=prepared.provider,
            manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
            authority=ctx.service,
            targets=prepared.targets,
        )
        return await guard.authorize(body, prepared.claims)

    async def run_reservation():
        async with ctx.factory() as session:
            outcome = await releases.reserve_native_release(
                session,
                context=context,
                state=state,
                workflow=body.workflow,
                environment="dev",
                initial=True,
                dispatch_revision=body.dispatch_revision,
                resolver=resolver,
                manifest_loader=lambda: SimpleNamespace(entry=lambda key: prepared.entry),
            )
            await session.commit()
            return outcome

    async with ctx.factory() as observer:
        anchored = await observer.get(OrchestrationNode, ctx.node.id, with_for_update=True, populate_existing=True)
        row = await observer.get(OrchestrationExecution, ctx.execution.id)
        assert anchored is not None and row is not None
        native_task = asyncio.create_task((run_guard if native == "guard" else run_reservation)(), name="native-under-test")
        try:
            await asyncio.wait_for(requesting.wait(), timeout=10)
            advanced = await advance_execution(
                observer,
                identity=ctx.identity,
                advance=PhaseAdvance(
                    phase=ExecutionPhase(row.phase),
                    status=ExecutionStatus(row.status),
                    expected_revision=row.revision,
                    next_check_at=datetime.now(UTC),
                ),
                pending_action_key=row.pending_action_key,
            )
            await observer.commit()
        except BaseException:
            native_task.cancel()
            raise
    (outcome,) = await asyncio.wait_for(asyncio.gather(native_task, return_exceptions=True), timeout=30)
    assert not isinstance(outcome, DBAPIError), f"native {native} inverted the observer node lock: {outcome!r}"
    assert advanced.kind is OutcomeKind.APPLIED
    if native == "guard":
        assert outcome is None
        async with ctx.factory() as session:
            action = await session.get(OrchestrationAction, "native-release-job")
            assert action.detail["release_job"] == {"run_id": "42", "run_attempt": "1"}
    else:
        assert isinstance(outcome, CycleBlockedError)
        assert "operations_shared_release_owner_changed" in str(outcome)
        async with ctx.factory() as session:
            intents = (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.operation_key.like("native-release:%")))).all()
            assert not intents
