"""D3 runs through real PostgreSQL, D2 handoff, protected grant and M2 identity."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from src.orchestration.deployment_controller import DEPLOYMENT_KIND, DeploymentController, DeploymentServices
from src.orchestration.deployment_runtime_contract import RuntimeComponent
from src.orchestration.deployment_target import ResolvedTarget
from src.orchestration.deployment_workflows import DeploymentWorkflows
from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.execution_state import ExecutionPhase, PhaseAdvance
from src.orchestration.execution_store import advance_execution, load_execution
from src.orchestration.models import OrchestrationAction, OrchestrationEnvironmentLease, OrchestrationWorkClaim
from src.orchestration.review_cycle import CycleBlockedError
from tests.orchestration.test_deployment_workflows import SOURCE, action, cycle, deployment, merge, pg_server, pg_url, state, store  # noqa: F401
from tests.orchestration.test_deployment_workflows import tick as workflow_tick

pytestmark = pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)
DIGEST = "sha256:" + "f" * 64


@pytest.fixture
async def runtime(deployment):  # noqa: F811
    return await prepare_runtime(deployment)


async def prepare_runtime(ctx):
    ctx.entry = replace(ctx.entry, verification_adapter="gateway-health-verification")
    ctx.runs.append(ctx.make_run())
    await workflow_tick(ctx)
    await workflow_tick(ctx)
    assert (await state(ctx))[0].phase == "awaiting_runtime_verification"
    ctx.provider.release = AsyncMock(return_value=(SimpleNamespace(), "f" * 64, "github/actions/runs/42/artifacts/88"))
    ctx.provider.contains = AsyncMock()
    ctx.component = RuntimeComponent(
        component="gateway-backend",
        actual_revision=ctx.entry.artifact_revision,
        artifact_hash="f" * 64,
        image_digest=DIGEST,
        healthy=True,
        evidence_ref="github/actions/runs/42/artifacts/88",
        observed_at=datetime.now(UTC),
        migration_head="head",
        tick_digest=DIGEST,
        pod_uids=["pod-uid"],
    )

    async def resolve(*args, **kwargs):
        if kwargs.get("authorize_scope"):
            kwargs["authorize_scope"]("delivery-connection", "arn:aws:iam::123456789012:role/test")
        # Provider I/O must not happen while ledger or environment rows are locked.
        async with ctx.factory() as db:
            await db.execute(select(OrchestrationEnvironmentLease).with_for_update(nowait=True))
        return ResolvedTarget(ctx.target, "delivery-connection", "test-role", "human", "arn:aws:iam::123456789012:role/test", [ctx.component])

    ctx.targets.resolve = AsyncMock(side_effect=resolve)
    ctx.runtime_services = DeploymentServices(
        ctx.factory,
        authority=ctx.service,
        provider=ctx.provider,
        targets=ctx.targets,
        manifest_loader=lambda: ctx.workflow_services.manifest_loader(),
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
        handlers={
            ExecutionPhase.AWAITING_RUNTIME_VERIFICATION: DeploymentController(ctx.factory, ctx.runtime_services),
            ExecutionPhase.DEPLOYMENT_PENDING: DeploymentWorkflows(ctx.factory, ctx.workflow_services),
        },
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="notice"),
        checkpoint=checkpoint,
    )


async def finish(ctx):
    result = await tick(ctx)
    assert result.advanced == 1, ((await state(ctx))[0].block_detail, result)
    assert (await state(ctx))[0].phase == "deployment_pending"
    await tick(ctx)
    result = await tick(ctx)
    assert result.advanced == 1, ((await state(ctx))[0].block_detail, result)


async def test_verified_release_releases_lease_and_advances_only_to_evaluation(runtime):
    ctx = runtime
    await finish(ctx)
    execution, _, node, _ = await state(ctx)
    assert execution.phase == "evaluation_pending" and node.state == "passed"
    async with ctx.factory() as db:
        lease = await db.scalar(select(OrchestrationEnvironmentLease))
        assert lease.state == "free" and lease.release_reason == "completed"
        receipts = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == DEPLOYMENT_KIND))).all())
        assert len(receipts) == 2
        final = next(a.detail["deployment_receipt"] for a in receipts if a.detail["deployment_receipt"]["delivery_complete"])
        assert final["source_revision"] == final["actual_revision"] == SOURCE
        assert final["components"][0]["tick_digest"] == DIGEST
    assert ctx.dispatches == []


@pytest.mark.parametrize(
    "failure",
    [
        "stale_runtime",
        "partial_migration",
        "unsupported",
        "workflow_failed",
        "attempt_changed",
        "wrong_claim",
        "wrong_merge",
        "missing_component",
        "expired",
    ],
)
async def test_false_green_cannot_release_lease_or_advance(runtime, failure):
    ctx = runtime
    if failure in {"stale_runtime", "partial_migration"}:
        ctx.targets.resolve.side_effect = CycleBlockedError("deployment_" + failure)
    elif failure == "unsupported":
        ctx.entry = replace(ctx.entry, verification_adapter="not-shipped")
    elif failure == "missing_component":
        ctx.targets.resolve.side_effect = None
        ctx.targets.resolve.return_value = ResolvedTarget(
            ctx.target, "delivery-connection", "test-role", "human", "arn:aws:iam::123456789012:role/test", []
        )
    elif failure == "attempt_changed":
        ctx.runs[0] = replace(ctx.runs[0], run_attempt=2)
    else:
        async with ctx.factory() as db:
            row = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "deployment_workflow"))
            detail = {**row.detail, "workflow_receipt": {**row.detail["workflow_receipt"]}}
            if failure == "workflow_failed":
                detail["workflow_receipt"]["conclusion"] = "failure"
            elif failure == "wrong_claim":
                detail["workflow_receipt"]["claim_generation"] += 1
            elif failure == "wrong_merge":
                detail["workflow_receipt"]["source_revision"] = "a" * 40
            else:
                detail["observation_deadline"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
            row.detail = detail
            await db.commit()
    await tick(ctx)
    execution = (await state(ctx))[0]
    assert execution.phase == "awaiting_runtime_verification" and execution.status == "blocked", execution.block_detail
    async with ctx.factory() as db:
        assert (await db.scalar(select(OrchestrationEnvironmentLease))).state == "held"
        assert await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == DEPLOYMENT_KIND)) is None
    assert ctx.dispatches == []


async def test_newer_authorized_containing_release_is_bound_to_actual_revision(runtime):
    ctx = runtime
    actual = "b" * 40
    ctx.entry = replace(ctx.entry, artifact_revision=actual)
    ctx.component = ctx.component.model_copy(update={"actual_revision": actual})
    ctx.runs[0] = replace(ctx.runs[0], context=ctx.runs[0].context.model_copy(update={"source_revision": actual, "workflow_revision": actual}))
    await finish(ctx)
    ctx.provider.contains.assert_awaited_once()
    checked_binding, checked_source, checked_actual = ctx.provider.contains.await_args.args
    assert (checked_binding.id, checked_source, checked_actual) == (ctx.binding.id, SOURCE, actual)
    assert (await action(ctx)).detail["deployment_receipt"]["actual_revision"] == actual


async def test_newer_unrelated_release_blocks(runtime):
    ctx = runtime
    ctx.entry = replace(ctx.entry, artifact_revision="b" * 40)
    ctx.provider.contains.side_effect = CycleBlockedError("deployment_newer_release_does_not_contain_merge")
    await tick(ctx)
    assert (await state(ctx))[0].status == "blocked"
    ctx.targets.resolve.assert_not_awaited()


async def test_retry_preserves_original_deadline(runtime):
    ctx = runtime
    ctx.targets.resolve.side_effect = httpx.ReadTimeout("temporary outage")
    await tick(ctx)
    assert (await state(ctx))[0].status == "awaiting_external"
    async with ctx.factory() as db:
        row = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "deployment_workflow"))
        row.detail = {**row.detail, "observation_deadline": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
        await db.commit()
    await tick(ctx)
    assert "deadline_reached" in (await state(ctx))[0].block_detail
    assert ctx.targets.resolve.await_count == 1


async def test_changed_claim_before_settlement_cannot_release(runtime):
    ctx = runtime
    original = ctx.targets.resolve.side_effect

    async def changed(*args, **kwargs):
        value = await original(*args, **kwargs)
        async with ctx.factory() as db:
            claim = await db.get(OrchestrationWorkClaim, ctx.identity.claim_id)
            claim.generation += 1
            await db.commit()
        return value

    ctx.targets.resolve.side_effect = changed
    await tick(ctx)
    async with ctx.factory() as db:
        assert (await db.scalar(select(OrchestrationEnvironmentLease))).state == "held"


async def test_concurrent_duplicate_observations_settle_once(runtime):
    ctx = runtime
    await asyncio.gather(tick(ctx), tick(ctx))
    async with ctx.factory() as db:
        actions = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == DEPLOYMENT_KIND))).all())
        assert len(actions) == 1
        assert (await db.scalar(select(OrchestrationEnvironmentLease))).state == "free"


async def test_docs_only_receipt_has_no_fabricated_runtime(deployment):  # noqa: F811
    ctx = deployment
    ctx.entry = replace(ctx.entry, docs_only=True, component_selectors=("documentation",), artifact_revision=None)
    ctx.provider.changed_files.return_value = ("docs/readme.md",)
    await workflow_tick(ctx)
    ctx.runtime_services = DeploymentServices(
        ctx.factory, authority=ctx.service, provider=ctx.provider, targets=ctx.targets, manifest_loader=ctx.workflow_services.manifest_loader
    )
    await tick(ctx)
    assert (await state(ctx))[0].phase == "evaluation_pending"
    async with ctx.factory() as db:
        row = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == DEPLOYMENT_KIND))
        receipt = row.detail["deployment_receipt"]
        assert receipt["docs_only"] and receipt["delivery_complete"] and receipt["components"] == []
    ctx.targets.resolve.assert_not_awaited()


async def test_two_manifest_entries_require_complete_component_evidence(runtime):
    from src.orchestration.deployment_manifest import DeploymentManifest

    ctx = runtime
    migration = replace(
        ctx.entry,
        entry_id="migrations",
        component_selectors=("gateway-migrations",),
        verification_adapter="alembic-single-head-verification",
        workflow=replace(ctx.entry.workflow, path=".github/workflows/run-gateway-migrations.yml"),
    )
    ctx.workflow_services.manifest_loader = lambda: DeploymentManifest(1, (ctx.entry, migration))
    ctx.provider.changed_files.return_value = ("modules/gateway/src/example.py", "modules/gateway/alembic/versions/example.py")
    await tick(ctx)
    assert (await state(ctx))[0].phase == "deployment_pending"
    await tick(ctx)
    await tick(ctx)
    assert (await state(ctx))[0].phase == "awaiting_runtime_verification"
    ctx.component = ctx.component.model_copy(update={"component": "gateway-migrations", "tick_digest": None})
    await finish(ctx)
    async with ctx.factory() as db:
        rows = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == DEPLOYMENT_KIND))).all())
        receipt = next(row.detail["deployment_receipt"] for row in rows if row.detail["deployment_receipt"]["delivery_complete"])
        assert {item["component"] for item in receipt["components"]} == {"gateway-backend", "gateway-migrations"}
        assert receipt["manifest_entry_ids"] == ["gateway", "migrations"]
        assert len(receipt["workflow_operation_keys"]) == 2


async def test_foreign_lease_holder_cannot_be_released(runtime):
    ctx = runtime
    async with ctx.factory() as db:
        lease = await db.scalar(select(OrchestrationEnvironmentLease))
        lease.owner_generation += 1
        await db.commit()
    await tick(ctx)
    async with ctx.factory() as db:
        assert (await db.scalar(select(OrchestrationEnvironmentLease))).state == "held"
        assert await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == DEPLOYMENT_KIND)) is None


async def test_final_readback_preserves_expiry_and_hides_credential_inputs(runtime):
    from src.orchestration.deployment_controller import bounded_deployment_summary

    ctx = runtime
    await finish(ctx)
    async with ctx.factory() as db:
        rows = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == DEPLOYMENT_KIND))).all())
        final = next(row for row in rows if row.detail["deployment_receipt"]["delivery_complete"])
        summary = bounded_deployment_summary(final.detail)
        assert summary["actual_revision"] == SOURCE and summary["delivery_complete"]
        assert set(summary) == {
            "source_revision",
            "actual_revision",
            "manifest_entry_ids",
            "delivery_complete",
            "docs_only",
            "observed_at",
            "valid_until",
        }
        partial = next(row for row in rows if not row.detail["deployment_receipt"]["delivery_complete"])
        assert final.detail["deployment_receipt"]["valid_until"] == partial.detail["deployment_receipt"]["valid_until"]
