"""D2 runs through the real PostgreSQL ledger, M2 receipt and protected grant."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select, text

from src.agentauth.bootstrap import BootstrapRefusedError
from src.agentauth.engine import validate_engine_authority
from src.orchestration.deployment_manifest import DeploymentManifest, EntryStatus, ManifestEntry, PhysicalTarget, TargetEvidence, WorkflowRef
from src.orchestration.deployment_target import ResolvedTarget
from src.orchestration.deployment_workflow_provider import WorkflowContext, WorkflowDefinition, WorkflowRun
from src.orchestration.deployment_workflows import PHASES, WORKFLOW_KIND, DeploymentWorkflows, WorkflowReceipt, WorkflowServices
from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.execution_state import ExecutionPhase, ExecutionStatus, PhaseAdvance
from src.orchestration.execution_store import advance_execution, load_execution
from src.orchestration.models import OrchestrationAction, OrchestrationEnvironmentLease, OrchestrationExecution, OrchestrationWorkClaim
from src.shared.models.base import Base
from tests.orchestration.test_merge_controller import merge  # noqa: F401
from tests.orchestration.test_merge_controller import tick as merge_tick
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, state, store  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
SOURCE = "c" * 40
PATH = ".github/workflows/gateway-deploy.yml"


@pytest.fixture
async def deployment(merge):  # noqa: F811
    ctx = merge
    assert (await merge_tick(ctx)).effects_succeeded == 1
    await merge_tick(ctx)
    assert (await state(ctx))[0].phase == "deployment_pending"
    async with ctx.factory() as db:
        connection = await db.connection()
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[OrchestrationEnvironmentLease.__table__]))
        await db.commit()
    ctx.target = PhysicalTarget(
        "aws", "123456789012", "us-east-1", "eks-namespace", "cluster/namespace", TargetEvidence("test-sts", datetime.now(UTC).isoformat())
    )
    inputs = {"environment": "dev", "customer_account_id": ctx.target.account_id, "customer_user_id": "human", "customer_aws_label": "test-role"}
    workflow = WorkflowRef(PATH, SOURCE, {k: frozenset({v}) for k, v in inputs.items()}, "adp_correlation")
    ctx.entry = ManifestEntry(
        entry_id="gateway",
        status=EntryStatus.ENABLED,
        connection_id="delivery-connection",
        component_selectors=("gateway-backend",),
        workflow=workflow,
        resource_kind=ctx.target.resource_kind,
        resource_id=ctx.target.resource_id,
        artifact_revision=SOURCE,
        verification_adapter="gateway",
    )
    definition = WorkflowDefinition(SOURCE, SOURCE, "d" * 40, inputs, True, "main", SOURCE)
    ctx.definition = definition
    ctx.runs, ctx.dispatches = [], []
    ctx.timeout_after_dispatch = False
    ctx.provider = SimpleNamespace(
        changed_files=AsyncMock(return_value=("modules/gateway/src/example.py",)),
        definition=AsyncMock(side_effect=lambda *args, for_dispatch=True: definition if for_dispatch else replace(definition, dispatch_ref="")),
    )

    def run(status="completed", conclusion="success"):
        context = WorkflowContext(
            schema_version=1,
            repository_id=123,
            run_id=42,
            run_attempt=1,
            workflow_path=PATH,
            workflow_revision=SOURCE,
            source_revision=SOURCE,
            account_id=ctx.target.account_id,
            region=ctx.target.region,
            resource_kind=ctx.target.resource_kind,
            resource_id=ctx.target.resource_id,
            inputs=inputs,
            correlation="",
        )
        return WorkflowRun(
            42, 1, status, conclusion, f"https://github.com/{ctx.binding.repo}/actions/runs/42", context, 77, "e" * 64, datetime.now(UTC).isoformat()
        )

    async def observe(*args, **kwargs):
        return (ctx.runs[0] if ctx.runs else None), False

    async def dispatch(*args, **kwargs):
        await kwargs["reauthorize"]()
        # The external call can acquire every ledger/lease row immediately:
        # no controller transaction holds a lock across GitHub I/O.
        async with ctx.factory() as db:
            await db.execute(text("SET LOCAL lock_timeout = '250ms'"))
            await db.execute(select(OrchestrationExecution).with_for_update(nowait=True))
            await db.execute(select(OrchestrationEnvironmentLease).with_for_update(nowait=True))
        ctx.dispatches.append(kwargs["correlation"])
        ctx.runs.append(run())
        if ctx.timeout_after_dispatch:
            raise httpx.ReadTimeout("response lost after dispatch")

    ctx.make_run = run
    ctx.provider.observe = AsyncMock(side_effect=observe)
    ctx.provider.dispatch = AsyncMock(side_effect=dispatch)
    ctx.targets = SimpleNamespace(
        resolve=AsyncMock(return_value=ResolvedTarget(ctx.target, "delivery-connection", "test-role", "human", "arn:aws:iam::123456789012:role/test"))
    )
    ctx.workflow_services = WorkflowServices(
        ctx.factory, authority=ctx.service, provider=ctx.provider, targets=ctx.targets, manifest_loader=lambda: DeploymentManifest(1, (ctx.entry,))
    )
    return ctx


async def tick(ctx, checkpoint=None):
    async with ctx.factory() as db:
        record = (await load_execution(db, identity=ctx.identity)).record
        await advance_execution(
            db,
            identity=ctx.identity,
            advance=PhaseAdvance(phase=record.phase, status=record.status, expected_revision=record.revision, next_check_at=datetime.now(UTC)),
        )
        await db.commit()
    return await run_execution_runner(
        ctx.factory,
        handlers=dict.fromkeys(PHASES, DeploymentWorkflows(ctx.factory, ctx.workflow_services)),
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="notice"),
        checkpoint=checkpoint,
    )


async def action(ctx):
    async with ctx.factory() as db:
        return await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == WORKFLOW_KIND))


async def test_automatic_workflow_adopted_without_second_dispatch(deployment):
    ctx = deployment
    ctx.runs.append(ctx.make_run())
    first = await tick(ctx)
    assert first.effects_succeeded == 1, (first, (await state(ctx))[0].block_detail, (await action(ctx)).detail if await action(ctx) else None)
    await tick(ctx)
    execution, _, node, _ = await state(ctx)
    assert execution.phase == "awaiting_runtime_verification" and node.state == "passed"
    assert ctx.dispatches == []
    receipt = WorkflowReceipt.model_validate((await action(ctx)).detail["workflow_receipt"])
    assert receipt.run_id == 42 and receipt.source_revision == SOURCE
    async with ctx.factory() as db:
        lease = await db.scalar(select(OrchestrationEnvironmentLease))
        assert lease.owner_action_id == receipt.lease_holder_action_id


async def test_timeout_after_success_restart_uses_recorded_definition(deployment):
    ctx = deployment
    ctx.timeout_after_dispatch = True
    result = await tick(ctx)
    assert result.effects_uncertain == 1, (result, (await state(ctx))[0].block_detail, (await action(ctx)).detail if await action(ctx) else None)
    ctx.provider.definition.side_effect = AssertionError("A later branch definition cannot replace historical dispatch evidence")
    ctx.targets.resolve.side_effect = AssertionError("Reconciliation does not mint deployment credentials")
    ctx.workflow_services.manifest_loader = lambda: (_ for _ in ()).throw(AssertionError("Historical approval is durable"))
    await tick(ctx)
    assert (await state(ctx))[0].phase == "awaiting_runtime_verification"
    assert len(ctx.dispatches) == 1


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
async def test_terminal_workflow_outcomes_preserve_evidence_for_d3(deployment, conclusion):
    ctx = deployment
    ctx.runs.append(ctx.make_run(conclusion=conclusion))
    await tick(ctx)
    await tick(ctx)
    assert WorkflowReceipt.model_validate((await action(ctx)).detail["workflow_receipt"]).conclusion == conclusion
    assert (await state(ctx))[0].phase == "awaiting_runtime_verification"


async def test_unknown_dispatch_retains_lease_and_does_not_redispatch(deployment):
    ctx = deployment
    ctx.timeout_after_dispatch = True
    await tick(ctx)
    ctx.runs.clear()
    await tick(ctx)
    await tick(ctx)
    assert len(ctx.dispatches) == 1
    assert (await state(ctx))[0].phase == "deployment_pending"
    async with ctx.factory() as db:
        assert await db.scalar(select(OrchestrationEnvironmentLease)) is not None


async def test_claim_generation_change_blocks_effect(deployment):
    ctx = deployment
    async with ctx.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, ctx.identity.claim_id)
        claim.generation += 1
        await db.commit()
    await tick(ctx)
    assert ctx.dispatches == []


async def test_passed_worker_cannot_resume_without_engine_delivery_identity(deployment):
    ctx = deployment
    _, claim, _, _ = await state(ctx)
    raw = ctx.store._read(f"TENANT#{ctx.identity.org_id}", f"EXEC#{claim.active_run_id}")
    grant = ctx.store.live_grant(
        invocation_id=claim.active_run_id, tenant_id=ctx.identity.org_id, attempt=int(raw["current_attempt"]["N"]), now=datetime.now(UTC)
    )
    async with ctx.factory() as db:
        with pytest.raises(BootstrapRefusedError, match="node is no longer authorized"):
            await validate_engine_authority(session=db, execution=raw, grant=grant, store=ctx.store)
        await validate_engine_authority(session=db, execution=raw, grant=grant, store=ctx.store, delivery_identity=ctx.identity)
        with pytest.raises(BootstrapRefusedError, match="terminal worker"):
            await validate_engine_authority(
                session=db, execution={**raw, "status": {"S": "running"}}, grant=grant, store=ctx.store, delivery_identity=ctx.identity
            )


async def test_revocation_after_intent_prevents_workflow_dispatch(deployment):
    ctx = deployment

    async def revoke(stage, context):
        if stage == "after_intent":
            _, claim, _, _ = await state(ctx)
            raw = ctx.store._read(f"TENANT#{ctx.identity.org_id}", f"EXEC#{claim.active_run_id}")
            raw["status"] = {"S": "revoked"}
            ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)

    await tick(ctx, revoke)
    assert ctx.dispatches == []
    assert (await action(ctx)).status == "failed"


@pytest.mark.parametrize("lapsed", [False, True])
async def test_competing_target_lease_blocks_dispatch_even_when_lapsed(deployment, lapsed):
    from src.orchestration.environment_leases import LeaseHolder, acquire_lease

    ctx = deployment
    async with ctx.factory() as db:
        leased = await acquire_lease(
            db, target=ctx.target, holder=LeaseHolder("other-tenant", "other-action", 1), manifest_entry_id="other", release_ref="d" * 40
        )
        assert leased.applied
        if lapsed:
            lease = await db.scalar(select(OrchestrationEnvironmentLease))
            lease.lease_expires_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
    await tick(ctx)
    assert ctx.dispatches == []
    assert "deployment_target_lease" in (await action(ctx)).detail["observation"]


async def test_concurrent_ticks_dispatch_once(deployment):
    import asyncio

    ctx = deployment
    await asyncio.gather(tick(ctx), tick(ctx))
    assert len(ctx.dispatches) == 1
    await tick(ctx)
    assert (await state(ctx))[0].phase == "awaiting_runtime_verification"


async def test_observation_after_last_allowed_attempt_still_advances(deployment):
    ctx = deployment
    await tick(ctx)
    async with ctx.factory() as db:
        record = await db.get(OrchestrationExecution, ctx.execution.id)
        record.attempts = ctx.policy.limits.max_attempts_per_node
        await db.commit()
    await tick(ctx)
    assert (await state(ctx))[0].phase == "awaiting_runtime_verification"
    assert len(ctx.dispatches) == 1


async def test_documentation_only_handoff_never_dispatches(deployment):
    ctx = deployment
    ctx.entry = replace(ctx.entry, docs_only=True, component_selectors=("documentation",), artifact_revision=None)
    ctx.provider.changed_files.return_value = ("docs/guide.md",)
    result = await tick(ctx)
    assert result.advanced == 1, result
    assert (await state(ctx))[0].phase == "awaiting_runtime_verification"
    assert ctx.dispatches == []
    async with ctx.factory() as db:
        handoff = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "deployment_handoff"))
        assert handoff.detail["handoff_reason"] == "documentation_only" and handoff.status == "succeeded"


async def test_workflow_receipt_projection_is_bounded(deployment):
    from src.orchestration.deployment_workflows import bounded_workflow_summary

    await tick(deployment)
    await tick(deployment)
    summary = bounded_workflow_summary((await action(deployment)).detail)
    assert summary["run_id"] == 42 and summary["conclusion"] == "success"
    assert "inputs" not in summary and "target" not in summary
    assert bounded_workflow_summary({"workflow_receipt": {"run_url": "x" * 10000}}) is None


async def test_selected_entries_wait_for_d3_and_adopt_existing_migration_run(deployment):
    from src.orchestration.environment_leases import LeaseHolder, ReleaseReason, release_lease

    ctx = deployment
    migration = replace(
        ctx.entry,
        entry_id="migrations",
        component_selectors=("gateway-migrations",),
        workflow=replace(ctx.entry.workflow, path=".github/workflows/run-gateway-migrations.yml"),
    )
    ctx.workflow_services.manifest_loader = lambda: DeploymentManifest(1, (ctx.entry, migration))
    ctx.provider.changed_files.return_value = ("modules/gateway/src/example.py", "modules/gateway/alembic/versions/060_example.py")
    await tick(ctx)
    await tick(ctx)
    first = await action(ctx)
    assert first.detail["workflow_receipt"]["remaining_entry_ids"] == ["migrations"]
    # The receipt is D3's boundary; another D2 tick cannot advance it itself.
    await tick(ctx)
    assert len(ctx.dispatches) == 1

    async with ctx.factory() as db:
        row = await db.get(OrchestrationAction, first.id)
        row.detail = {**row.detail, "runtime_verified": True}
        released = await release_lease(
            db,
            canonical_target_key=ctx.target.canonical_key,
            holder=LeaseHolder(ctx.identity.org_id, row.id, ctx.identity.claim_generation),
            reason=ReleaseReason.COMPLETED,
            terminal_evidence="test D3 runtime evidence",
        )
        assert released.applied
        record = (await load_execution(db, identity=ctx.identity)).record
        await advance_execution(
            db,
            identity=ctx.identity,
            advance=PhaseAdvance(
                phase=ExecutionPhase.DEPLOYMENT_PENDING,
                status=ExecutionStatus.RUNNABLE,
                expected_revision=record.revision,
                next_check_at=datetime.now(UTC),
            ),
        )
        await db.commit()
    ctx.runs[0] = replace(ctx.runs[0], context=ctx.runs[0].context.model_copy(update={"workflow_path": migration.workflow.path}))
    await tick(ctx)
    await tick(ctx)
    assert len(ctx.dispatches) == 1
    async with ctx.factory() as db:
        rows = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == WORKFLOW_KIND))).all())
        assert {row.detail["manifest_entry_id"] for row in rows} == {"gateway", "migrations"}
        assert all(row.detail["workflow_receipt"]["run_id"] == 42 for row in rows)


async def test_transient_observation_retains_original_deadline_and_lease(deployment):
    ctx = deployment
    await tick(ctx)
    ctx.provider.observe.side_effect = httpx.ReadTimeout("temporary outage")
    await tick(ctx)
    assert (await state(ctx))[0].status == "awaiting_external"
    async with ctx.factory() as db:
        row = await db.get(OrchestrationAction, (await action(ctx)).id)
        row.detail = {**row.detail, "observation_deadline": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
        await db.commit()
    await tick(ctx)
    assert (await state(ctx))[0].status == "blocked"
    assert len(ctx.dispatches) == 1
    async with ctx.factory() as db:
        assert await db.scalar(select(OrchestrationEnvironmentLease)) is not None


async def test_receipt_keeps_actual_automatic_inputs(deployment):
    ctx = deployment
    automatic = ctx.make_run()
    actual = {**automatic.context.inputs, "customer_account_id": ""}
    ctx.runs.append(replace(automatic, context=automatic.context.model_copy(update={"inputs": actual})))
    await tick(ctx)
    await tick(ctx)
    assert WorkflowReceipt.model_validate((await action(ctx)).detail["workflow_receipt"]).inputs == actual
