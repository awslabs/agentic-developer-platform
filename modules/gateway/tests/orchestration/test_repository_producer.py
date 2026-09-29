"""Actual admission/runner recovery for a single explicitly authorized scan."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.deployment_workflow_provider import WorkflowContext, WorkflowRun
from src.orchestration.evaluation_acceptance import EvaluationAcceptanceError
from src.orchestration.evaluation_controller import EvaluationController
from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.execution_state import ExecutionPhase
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationNode,
    OrchestrationWorkClaim,
)
from src.orchestration.repository_evaluation import observe_repository_evaluation
from src.orchestration.repository_producer import CONTEXT_KIND, PRODUCER_KIND, RepositoryScanProvider
from src.orchestration.repository_producer_controller import RepositoryProducerController
from src.orchestration.run_reports import OrchestrationRunReport
from tests.orchestration.test_evaluation_acceptance import accept, contract_request  # noqa: F401
from tests.orchestration.test_repository_evaluation import repository_evaluation  # noqa: F401
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.fixture
async def scan(contract_request, monkeypatch):  # noqa: F811
    ctx = contract_request
    document = deepcopy(ctx.spec)
    document["workflows"] = [
        dict(
            criterion_id="final-scan",
            path=".github/workflows/scan.yml",
            source=dict(predecessor=document["predecessors"][0]["address"]),
            definition=dict(revision="f" * 40),
            required_jobs=["Scan"],
            artifacts=[
                dict(
                    name="scan-{run_attempt}",
                    path="scan-receipt.json",
                    predicates=[dict(criterion_id="coverage", pointer="/coverage_complete", operation="equals", expected=True)],
                )
            ],
        )
    ]
    document["producer"] = dict(
        mode="dispatch_once",
        inputs=dict(expected_account_id="123456789012", region="us-east-1"),
        workflow_criterion_id="final-scan",
        target=dict(account_id="123456789012", region="us-east-1", resource_kind="repository_scan", resource_id=ctx.binding.repo),
        receipt_artifact="scan-{run_attempt}",
        receipt_path="scan-receipt.json",
        images={
            "controller": {
                "grype": dict(digest="sha256:" + "a" * 64, provenance_sha256="b" * 64),
                "syft": dict(digest="sha256:" + "c" * 64, provenance_sha256="d" * 64),
            }
        },
    )
    ctx.request = ctx.request.model_copy(update=dict(specification=document, authorize_workflow_dispatch=True))
    ctx.provider_scan = RepositoryScanProvider(evidence=ctx.provider)
    ctx.provider_scan.observe = AsyncMock(return_value=(None, False))

    async def preflight(self, binding, spec, sources):
        return dict(
            source_revision=sources[0]["merge_sha"],
            definition=dict(
                approved_revision="f" * 40,
                source_revision=sources[0]["merge_sha"],
                blob_sha="e" * 40,
                defaults=dict(adp_correlation="", adp_source_revision="", adp_definition_revision="", **spec.producer.inputs),
                dispatchable=True,
                dispatch_ref="main",
                dispatch_revision="f" * 40,
                caller_path=None,
                caller_blob_sha=None,
            ),
            workflow=dict(
                path=".github/workflows/scan.yml",
                definition_revision="f" * 40,
                allowed_inputs={key: [value] for key, value in spec.producer.inputs.items()},
                correlation_input="adp_correlation",
            ),
        )

    monkeypatch.setattr(RepositoryScanProvider, "preflight", preflight)
    ctx.controller = EvaluationController(ctx.factory)
    ctx.controller.repository_producer = RepositoryProducerController(ctx.factory, provider=ctx.provider_scan)
    return ctx


async def admit(ctx):
    async with ctx.factory() as db:
        result, _ = await accept(ctx, db)
        await db.commit()
        node = await db.get(OrchestrationNode, ctx.eval_id)
        await observe_repository_evaluation(db, node, provider=ctx.provider)
        await db.commit()
        assert node.state == "running", [
            row.reason for row in await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id))
        ]
        ctx.acceptance_id = result["decision_id"]
        ctx.scan_execution = await db.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == ctx.eval_id))


async def tick(ctx):
    async with ctx.factory() as db:
        row = await db.get(OrchestrationExecution, ctx.scan_execution.id)
        row.next_check_at = datetime.now(UTC)
        await db.commit()
    return await run_execution_runner(
        ctx.factory,
        handlers={ExecutionPhase.EVALUATION_PENDING: ctx.controller},
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="test-notice"),
    )


async def test_scan_dispatch_requires_separate_explicit_authorization(scan):
    async with scan.factory() as db:
        with pytest.raises(EvaluationAcceptanceError, match="explicit_producer_authorization_required"):
            await accept(scan, db, scan.request.model_copy(update={"authorize_workflow_dispatch": False}))
        assert await db.scalar(select(OrchestrationAction.id).where(OrchestrationAction.kind == CONTEXT_KIND)) is None


async def test_admission_creates_real_evaluation_execution_and_claim_without_worker_assignment(scan):
    await admit(scan)
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        claim = await db.get(OrchestrationWorkClaim, scan.scan_execution.claim_id)
        assert node.attempts == 1
        assert await db.scalar(select(OrchestrationRunReport.run_id).where(OrchestrationRunReport.node_id == node.id)) is None
        assert claim.owner_ref == node.flow_id and claim.issue_number == 999 and claim.state == "held"
        assert claim.active_run_id is None
        assert scan.scan_execution.phase == "evaluation_pending"


async def test_uncertain_post_is_never_repeated_by_subsequent_runner_ticks(scan):
    sent = []

    async def dispatch(binding, **kwargs):
        await kwargs["reauthorize"]()
        sent.append(kwargs["correlation"])
        raise TimeoutError("Response lost after provider accepted the dispatch")

    scan.provider_scan.dispatch = dispatch
    await admit(scan)
    await tick(scan)
    async with scan.factory() as db:
        rows = list(await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == PRODUCER_KIND)))
        assert len(rows) == 1 and rows[0].detail.get("dispatch_started"), [(row.status, row.detail) for row in rows]
    assert len(sent) == 1
    await tick(scan)
    await tick(scan)
    assert len(sent) == 1
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        assert node.state == "running" and node.attempts == 1


@pytest.mark.parametrize("human", [False, True])
@pytest.mark.parametrize("success", [True, False])
@pytest.mark.parametrize("after_dispatch", [None, "budget_exhausted", "deadline_expired"])
async def test_terminal_scan_releases_real_claim_and_only_verified_evidence_passes_node(scan, success, after_dispatch, human):
    if human:
        document = deepcopy(scan.request.specification)
        document.update(evidence_schema="workflow-evaluation/v1", acceptance_mode="human")
        document["workflows"][0].update(
            path=".github/workflows/eval-cli-uplift.yml",
            source={"revision": "f" * 40},
            required_jobs=["Live evaluation (dev)", "Recovery sweep (this run, plus anything expired)"],
        )
        producer = document["producer"]
        producer.pop("images")
        producer["target"].update(resource_kind="cli-evaluation", resource_id="dev")
        producer["inputs"] = dict(
            environment="dev", expected_revision="f" * 40, mode="start", fixtures_json="{}", suites="knowledge", evaluation_id="", inject_fault="none"
        )
        scan.request = scan.request.model_copy(update={"specification": document})
        async with scan.factory() as db:
            plan = await db.get(OrchestrationAcceptedPlan, scan.plan.id)
            amended = deepcopy(plan.plan_document)
            amended["execution_policy"]["evaluation_acceptance"] = {key: "human" for key in amended["execution_policy"]["evaluation_acceptance"]}
            plan.plan_document = amended
            await db.commit()

    async def dispatch(binding, **kwargs):
        await kwargs["reauthorize"]()

    scan.provider_scan.dispatch = dispatch
    await admit(scan)
    await tick(scan)
    async with scan.factory() as db:
        action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == PRODUCER_KIND))
        data = action.detail
    target = scan.request.specification["producer"]["target"]
    workflow_context = WorkflowContext(
        schema_version=1,
        repository_id=123,
        run_id=10,
        run_attempt=1,
        workflow_path=".github/workflows/scan.yml",
        workflow_revision="f" * 40,
        source_revision=data["source_revision"],
        inputs={},
        correlation=data["correlation"],
        **target,
    )
    run = WorkflowRun(
        10,
        1,
        "completed",
        "success",
        "https://github.com/o/r/actions/runs/10",
        workflow_context,
        12,
        "a" * 64,
        datetime.now(UTC).isoformat(),
    )
    scan.provider_scan.observe.return_value = (run, False)

    async def verify(binding, spec, sources):
        proof = await scan.provider.observe(binding, spec, sources)
        return proof["pull_requests"], {item["address"]: item["merge_sha"] for item in sources if item.get("address")}

    scan.provider.verify_sources = AsyncMock(side_effect=verify)
    scan.provider.workflow = AsyncMock(
        return_value=dict(
            criterion_id="final-scan",
            workflow_path=".github/workflows/scan.yml",
            source_revision=data["source_revision"],
            definition_revision="f" * 40,
            workflow_blob_sha="e" * 40,
            run_id=10,
            run_attempt=1,
            event="workflow_dispatch",
            jobs=[dict(name="Scan", job_id=11)],
            artifacts=[dict(artifact_id=13, name="scan-1", digest="d" * 64, path="scan-receipt.json", sha256="e" * 64)],
            criteria=[dict(criterion_id="coverage", passed=success)],
        )
    )
    if after_dispatch == "budget_exhausted":
        scan.spend = Decimal("99999")
    elif after_dispatch == "deadline_expired":
        async with scan.factory() as db:
            execution = await db.get(OrchestrationExecution, scan.scan_execution.id)
            execution.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()
    report = await tick(scan)
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        execution = await db.get(OrchestrationExecution, scan.scan_execution.id)
        claim = await db.get(OrchestrationWorkClaim, scan.scan_execution.claim_id)
        assert node.state == (("awaiting_gate" if human else "passed") if success else "failed"), (report, execution.status)
        assert execution.status == "concluded" and claim.state == "released"
        assert claim.release_reason == ("completed" if success else "failed")
        if success:
            row = await db.scalar(
                select(OrchestrationDecision)
                .where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "result_observed")
                .order_by(OrchestrationDecision.created_at.desc())
                .limit(1)
            )
            assert '"live_attestation":false' in row.reason


async def test_budget_exhausted_after_admission_refuses_actual_provider_post(scan):
    scan.provider_scan.dispatch = AsyncMock()
    await admit(scan)
    scan.spend = Decimal("99999")
    await tick(scan)
    scan.provider_scan.dispatch.assert_not_awaited()
    async with scan.factory() as db:
        rows = list(await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == PRODUCER_KIND)))
        assert all(not row.detail.get("dispatch_started") for row in rows)
        assert (await db.get(OrchestrationNode, scan.eval_id)).state == "running"


@pytest.mark.parametrize("changed_during_preflight", [False, True])
async def test_stale_harness_is_refused_before_claim_or_execution(scan, monkeypatch, changed_during_preflight):
    async with scan.factory() as db:
        await accept(scan, db)
        await db.commit()
    if changed_during_preflight:
        original = RepositoryScanProvider.preflight

        async def change_harness(*args):
            prepared = await original(*args)
            monkeypatch.setattr("src.orchestration.repository_producer.harness_digest", lambda: "0" * 64)
            return prepared

        monkeypatch.setattr(RepositoryScanProvider, "preflight", change_harness)
    else:
        monkeypatch.setattr("src.orchestration.repository_producer.harness_digest", lambda: "0" * 64)
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        await observe_repository_evaluation(db, node, provider=scan.provider)
        await db.commit()
        assert node.state == "ready" and node.attempts == 0
        assert await db.scalar(select(OrchestrationExecution.id).where(OrchestrationExecution.node_id == node.id)) is None
        assert await db.scalar(select(OrchestrationWorkClaim.id).where(OrchestrationWorkClaim.issue_number == 999)) is None


async def test_failed_workflow_without_cleanup_proof_retains_claim_and_never_redispatches(scan):
    async def dispatch(binding, **kwargs):
        await kwargs["reauthorize"]()

    scan.provider_scan.dispatch = AsyncMock(side_effect=dispatch)
    await admit(scan)
    await tick(scan)
    # Failure at GitHub is not proof that the asynchronous AWS child scan ended.
    from types import SimpleNamespace

    scan.provider_scan.observe.return_value = (SimpleNamespace(run_id=10, status="completed", conclusion="failure"), False)
    await tick(scan)
    await tick(scan)
    scan.provider_scan.dispatch.assert_awaited_once()
    async with scan.factory() as db:
        node = await db.get(OrchestrationNode, scan.eval_id)
        execution = await db.get(OrchestrationExecution, scan.scan_execution.id)
        claim = await db.get(OrchestrationWorkClaim, scan.scan_execution.claim_id)
        assert node.state == "running" and execution.status == "blocked" and claim.state == "held"
        assert execution.pending_action_key
