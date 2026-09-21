"""The CLI adapter uses real acceptance/claim/action ledgers with its own grant."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import select

from src.orchestration.cli_live_contract import NIGHTLY_SCHEDULE, CliLiveSpecification
from src.orchestration.deployment_workflow_provider import WorkflowContext, WorkflowDefinition, WorkflowRun
from src.orchestration.evaluation_acceptance import EvaluationAcceptanceError
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.repository_evaluation import observe_repository_evaluation
from src.orchestration.repository_producer import CLI_PRODUCER_KIND, RepositoryScanProvider
from src.orchestration.review_cycle import CycleBlockedError
from tests.orchestration.test_cli_live_evidence import cli_evidence, digest, validate  # noqa: F401
from tests.orchestration.test_evaluation_acceptance import accept, contract_request  # noqa: F401
from tests.orchestration.test_repository_evaluation import repository_evaluation  # noqa: F401
from tests.orchestration.test_repository_producer import admit, scan, tick  # noqa: F401
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


def request_for(ctx, evidence, *, dispatch=False):
    document = evidence.spec.model_dump(mode="json")
    document["predecessors"] = ctx.spec["predecessors"]
    document["runner"]["repository"] = ctx.binding.repo
    if dispatch:
        q = document["qualification"]
        document["producer"] = dict(
            mode="dispatch_once",
            workflow_criterion_id=document["workflows"][0]["criterion_id"],
            target=dict(
                account_id=q["deployment"]["account_id"],
                region=q["deployment"]["region"],
                resource_kind="cli_qualification",
                resource_id=ctx.binding.repo,
            ),
            inputs=dict(
                expected_account_id=q["deployment"]["account_id"],
                region=q["deployment"]["region"],
                qualification_contract="cli-live-qualification/v1",
                qualification_sha256=digest(q),
            ),
            receipt_artifact=q["manifest_artifact"],
            receipt_path=q["manifest_path"],
        )
    return ctx.request.model_copy(
        update=dict(specification=document, authorize_workflow_dispatch=False, authorize_cli_qualification_dispatch=dispatch)
    )


async def set_owner(ctx, owner=5644):
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        node.issue_ref = str(owner)
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"][-1]["issue_ref"] = str(owner)
        plan.plan_document = document
        ctx.saved_plan = deepcopy(document)
        await db.commit()


async def test_native_cli_observation_records_live_receipt_without_inventing_deployment_or_worker(contract_request, cli_evidence):  # noqa: F811
    ctx = contract_request
    await set_owner(ctx)
    ctx.request = request_for(ctx, cli_evidence)
    original = ctx.provider.observe.side_effect
    manifest, criteria = validate(cli_evidence)

    async def observe(binding, spec, sources):
        proof = await original(binding, spec, sources)
        proof["workflows"] = [
            dict(
                criterion_id="qualification-run",
                workflow_path=spec.workflows[0].path,
                source_revision="b" * 40,
                definition_revision="b" * 40,
                workflow_blob_sha="b" * 40,
                run_id=10,
                run_attempt=1,
                event="workflow_dispatch",
                jobs=[dict(name="live suite", job_id=12)],
                artifacts=[dict(artifact_id=13, name="qualification-1", digest="b" * 64, path="qualification.json", sha256="c" * 64)],
                criteria=criteria,
                qualification=manifest,
            )
        ]
        return proof

    ctx.provider.observe.side_effect = observe
    async with ctx.factory() as db:
        result, _ = await accept(ctx, db)
        await db.commit()
        node = await db.get(OrchestrationNode, ctx.eval_id)
        await observe_repository_evaluation(db, node, provider=ctx.provider)
        await db.commit()
        assert node.state == "passed" and node.attempts == 1
        row = await db.scalar(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "result_observed")
        )
        receipt = json.loads(row.reason)["receipt"]
        assert receipt["live_attestation"] is True and receipt["evidence_schema"] == "cli-live-evaluation-receipt/v1"
        assert receipt["acceptance_decision_id"] == result["decision_id"]
        assert len(receipt["workflows"][0]["criteria"]) == 124
        assert await db.scalar(select(OrchestrationWorkClaim.id).where(OrchestrationWorkClaim.issue_number == 999)) is None
        assert (await db.get(type(ctx.plan), ctx.plan.id)).plan_document == ctx.saved_plan
    ctx.claim_spy.assert_not_awaited()


@pytest.fixture
async def cli_dispatch(scan, cli_evidence, monkeypatch):  # noqa: F811
    ctx = scan
    await set_owner(ctx)
    ctx.request = request_for(ctx, cli_evidence, dispatch=True)
    original = RepositoryScanProvider.preflight

    async def preflight(self, binding, spec, sources):
        data = await original(self, binding, spec, sources)
        data["workflow"]["path"] = spec.workflows[0].path
        data["source_revision"] = data["definition"]["source_revision"] = spec.workflows[0].source.revision
        return data

    monkeypatch.setattr(RepositoryScanProvider, "preflight", preflight)
    return ctx


@pytest.mark.parametrize("grant", ["none", "scan"])
async def test_scan_grant_cannot_dispatch_cli_qualification(cli_dispatch, grant):
    ctx = cli_dispatch
    request = ctx.request.model_copy(update=dict(authorize_cli_qualification_dispatch=False, authorize_workflow_dispatch=grant == "scan"))
    async with ctx.factory() as db:
        with pytest.raises(EvaluationAcceptanceError):
            await accept(ctx, db, request)
        assert await db.scalar(select(OrchestrationAction.id).where(OrchestrationAction.kind == CLI_PRODUCER_KIND)) is None


async def test_cli_dispatch_uses_real_claim_and_durable_unknown_post_fence(cli_dispatch):
    ctx = cli_dispatch
    sent = []

    async def dispatch(binding, **kwargs):
        await kwargs["reauthorize"]()
        sent.append(kwargs["correlation"])
        raise TimeoutError("provider accepted request; response lost")

    ctx.provider_scan.dispatch = dispatch
    await admit(ctx)
    await tick(ctx)
    await tick(ctx)
    await tick(ctx)
    assert len(sent) == 1
    async with ctx.factory() as db:
        action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == CLI_PRODUCER_KIND))
        assert action and action.detail["dispatch_started"]
        claim = await db.get(OrchestrationWorkClaim, ctx.scan_execution.claim_id)
        assert claim.state == "held" and claim.active_run_id is None
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "running"


@pytest.mark.parametrize("attachment", [False, True])
async def test_live_acceptance_cannot_downgrade_without_plan_amendment(contract_request, cli_evidence, attachment):  # noqa: F811
    ctx = contract_request
    await set_owner(ctx)
    repository_request = ctx.request
    ctx.request = request_for(ctx, cli_evidence)
    async with ctx.factory() as db:
        if attachment:
            await accept(ctx, db)
            await db.commit()
        else:
            plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
            document = deepcopy(plan.plan_document)
            document["nodes"][-1]["evaluation"] = ctx.request.specification
            plan.plan_document = document
            await db.commit()
        with pytest.raises(EvaluationAcceptanceError, match="live_contract_cannot_be_replaced"):
            await accept(ctx, db, repository_request)


async def test_cli_acceptance_is_bound_to_evaluation_owner_issue(contract_request, cli_evidence):  # noqa: F811
    ctx = contract_request
    ctx.request = request_for(ctx, cli_evidence)
    async with ctx.factory() as db:
        with pytest.raises(EvaluationAcceptanceError, match="cli_qualification_owner_changed"):
            await accept(ctx, db)


@pytest.mark.parametrize("change", [None, "missing", "empty", "changed"])
async def test_cli_preflight_preserves_exact_existing_nightly_schedule(cli_evidence, change):  # noqa: F811
    evidence = cli_evidence
    ctx = SimpleNamespace(
        spec={"predecessors": []}, binding=SimpleNamespace(repo="o/r"), request=SimpleNamespace(model_copy=lambda update: SimpleNamespace(**update))
    )
    spec = CliLiveSpecification.model_validate(request_for(ctx, evidence, dispatch=True).specification)
    events = dict(workflow_dispatch={}, schedule=deepcopy(NIGHTLY_SCHEDULE), pull_request={})
    if change == "missing":
        events.pop("schedule")
    elif change == "empty":
        events["schedule"] = []
    elif change == "changed":
        events["schedule"] = [{"cron": "0 * * * *"}]
    workflow = {
        "on": events,
        "concurrency": {"group": "cli-live", "cancel-in-progress": False},
        "run-name": "${{ format('ADP deployment {0}', inputs.adp_correlation) }}",
    }
    reader = SimpleNamespace(
        verify_sources=AsyncMock(return_value=([], {})), definition_blob=AsyncMock(return_value=("d" * 40, yaml.safe_dump(workflow).encode()))
    )
    provider = RepositoryScanProvider(evidence=reader)
    defaults = dict(adp_correlation="", adp_source_revision="", adp_definition_revision="", **spec.producer.inputs)
    provider.definition = AsyncMock(return_value=WorkflowDefinition("b" * 40, "b" * 40, "d" * 40, defaults, True, "main", "b" * 40))
    if change:
        with pytest.raises(CycleBlockedError):
            await provider.preflight(ctx.binding, spec, [])
    else:
        result = await provider.preflight(ctx.binding, spec, [])
        assert result["workflow"]["path"] == spec.workflows[0].path


async def test_completed_cli_qualification_settles_with_typed_live_receipt_after_budget_exhaustion(cli_dispatch, cli_evidence):  # noqa: F811
    ctx = cli_dispatch

    async def dispatch(binding, **kwargs):
        await kwargs["reauthorize"]()

    ctx.provider_scan.dispatch = dispatch
    await admit(ctx)
    await tick(ctx)
    async with ctx.factory() as db:
        action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == CLI_PRODUCER_KIND))
        data = action.detail
    target = ctx.request.specification["producer"]["target"]
    context = WorkflowContext(
        schema_version=1,
        repository_id=123,
        run_id=10,
        run_attempt=1,
        workflow_path=cli_evidence.run["path"],
        workflow_revision="b" * 40,
        source_revision=data["source_revision"],
        inputs={},
        correlation=data["correlation"],
        **target,
    )
    run = WorkflowRun(10, 1, "completed", "success", "https://github.com/o/r/actions/runs/10", context, 12, "a" * 64, datetime.now(UTC).isoformat())
    ctx.provider_scan.observe.return_value = (run, False)

    async def verify(binding, spec, sources):
        proof = await ctx.provider.observe(binding, spec, sources)
        return proof["pull_requests"], {item["address"]: item["merge_sha"] for item in sources if item.get("address")}

    ctx.provider.verify_sources = AsyncMock(side_effect=verify)
    manifest, criteria = validate(cli_evidence)
    manifest["correlation"] = data["correlation"]
    ctx.provider.workflow = AsyncMock(
        return_value=dict(
            criterion_id="qualification-run",
            workflow_path=context.workflow_path,
            source_revision=data["source_revision"],
            definition_revision="b" * 40,
            workflow_blob_sha="b" * 40,
            run_id=10,
            run_attempt=1,
            event="workflow_dispatch",
            jobs=[dict(name="live suite", job_id=12)],
            artifacts=[dict(artifact_id=13, name="qualification-1", digest="b" * 64, path="qualification.json", sha256="c" * 64)],
            criteria=criteria,
            qualification=manifest,
        )
    )
    ctx.spend = Decimal("99999")
    await tick(ctx)
    async with ctx.factory() as db:
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "passed"
        assert (await db.get(OrchestrationWorkClaim, ctx.scan_execution.claim_id)).state == "released"
        row = await db.scalar(
            select(OrchestrationDecision)
            .where(OrchestrationDecision.node_id == ctx.eval_id, OrchestrationDecision.kind == "result_observed")
            .order_by(OrchestrationDecision.created_at.desc())
            .limit(1)
        )
        receipt = json.loads(row.reason)["receipt"]
        assert receipt["live_attestation"] is True and receipt["evidence_schema"] == "cli-live-evaluation-receipt/v1"
