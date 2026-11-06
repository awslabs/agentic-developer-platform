"""Synthetic merge settlement and provider observations for the amendment journey."""

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select

from src.admin.config import AdminRole
from src.orchestration.accepted_node import unresolved_required_inputs
from src.orchestration.compile import ApprovalContext
from src.orchestration.controls import GateDecisionRequest, approve_gate
from src.orchestration.dispatch_pass import run_dispatch_pass
from src.orchestration.execution_state import ExecutionIdentity
from src.orchestration.gate_execution import prepare_gate_execution
from src.orchestration.merge_controller import MERGE_KIND, MergeReceipt, code_only_delivery, settle_merge
from src.orchestration.models import (
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationExecution,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from src.orchestration.pr_bindings import PullRequestIdentity, register_binding, resolve_registration_target
from src.orchestration.repository_evaluation_provider import RepositoryEvidenceProvider
from src.orchestration.state import ActorKind
from src.orchestration.tick import run_tick
from tests.orchestration import test_repository_evaluation_provider as provider_fixture
from tests.orchestration.test_controls import _token_context
from tests.orchestration.test_policy_admission import _make_usage

provider_evidence = provider_fixture.evidence


async def settle_checker(session, *, node, config):
    """Supply a verified-merge fixture at the controller's settlement boundary."""
    execution = (await session.scalars(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))).one()
    identity = ExecutionIdentity(node.org_id, node.id, node.attempts, execution.accepted_plan_version, execution.claim_id, execution.claim_generation)
    dispatched = (
        await session.scalars(
            select(OrchestrationDecision).where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "node_dispatched")
        )
    ).one()
    run_id = json.loads(dispatched.reason)["run_id"]
    target = await resolve_registration_target(session, run_id=run_id, expected_org_id=node.org_id)
    binding, _ = await register_binding(
        session,
        target=target,
        pr=PullRequestIdentity(12345, "PR_2", config.repo, 2, "a" * 40),
        actor_id=run_id,
        actor_kind=ActorKind.SERVICE,
    )
    context = SimpleNamespace(identity=identity, execution=execution)
    now = datetime.now(UTC)
    receipt = MergeReceipt(
        **vars(identity),
        execution_id=execution.id,
        flow_id=node.flow_id,
        repo=binding.repo,
        pr_number=binding.pr_number,
        provider_repository_id=binding.provider_repository_id,
        provider_pr_node_id=binding.provider_pr_node_id,
        reviewed_head_sha=binding.head_sha,
        reviewed_base_sha=None,
        merge_sha="b" * 40,
        review_ref="fixture:verified-checker-review",
        operation_key="fixture:observed-checker-merge",
        method="external",
        verification_kind="post_merge_verification",
        eligibility_digest="c" * 64,
        eligibility_observed_at=now,
        merged_at=now,
        observed_at=now,
        adopted=True,
    )
    session.add(
        OrchestrationAction(org_id=node.org_id, execution_id=execution.id, operation_key=receipt.operation_key, kind=MERGE_KIND, status="succeeded")
    )
    await session.flush()
    await settle_merge(
        session,
        context,
        receipt,
        dict(
            binding_id=binding.id,
            binding_revision=binding.revision,
            accepted_scope=binding.accepted_scope,
            code_only=await code_only_delivery(session, context, node),
        ),
    )
    await _make_usage(session, node, flow_slug="demo-flow", cost_usd="1.00")
    await session.commit()
    return receipt


def cleanup_observations(data, repo):
    prefix = f"/repos/{repo}"
    responses = {path.replace("/repos/o/r", prefix).replace("scan.yml", "cleanup-validation.yml"): value for path, value in data.responses.items()}
    responses[prefix]["id"] = 12345
    pull = responses[prefix + "/pulls/2"]
    pull["head"]["repo"]["id"] = pull["base"]["repo"]["id"] = 12345
    checks = responses[prefix + "/commits/" + "a" * 40 + "/check-runs"]["check_runs"]
    checks[0]["name"] = "Unit tests"
    run = responses[prefix + "/actions/runs/10"]
    run["repository"]["id"] = run["head_repository"]["id"] = 12345
    run["path"] = ".github/workflows/cleanup-validation.yml"
    jobs = responses[prefix + "/actions/runs/10/attempts/2/jobs"]
    jobs["jobs"] = [{**jobs["jobs"][0], "name": "Validate cleanup"}]
    listing = responses[prefix + "/actions/workflows/cleanup-validation.yml/runs"]
    listing["workflow_runs"] = []
    artifact = responses[prefix + "/actions/runs/10/artifacts"]["artifacts"][0]
    artifact["name"] = "cleanup-result"
    calls = []

    def result(passed):
        observed = datetime.now(UTC).isoformat()
        run.update(run_started_at=observed, updated_at=observed)
        artifact["created_at"] = observed
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("result.json", json.dumps({"passed": passed}))
        data.binary = archive.getvalue()
        artifact.update(size_in_bytes=len(data.binary), digest="sha256:" + hashlib.sha256(data.binary).hexdigest())
        listing["workflow_runs"] = [run]

    def respond(request):
        assert request.method == "GET" and request.url.host == "api.github.com"
        calls.append(request.url.path)
        if request.url.path == prefix + "/actions/artifacts/13/zip":
            return httpx.Response(200, content=data.binary)
        assert request.url.path in responses, request.url.path
        return httpx.Response(200, json=responses[request.url.path])

    return SimpleNamespace(transport=httpx.MockTransport(respond), result=result, calls=calls, responses=responses, artifact=artifact)


async def approve_fixture_gate(session, *, gate, actor, plan_hash):
    access = SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=(AdminRole.ORG_ADMIN, actor.org_id, None)))
    preview = await prepare_gate_execution(session, gate=gate, actor=ApprovalContext(**vars(actor)))
    return await approve_gate(
        gate_id=gate.id,
        body=GateDecisionRequest(reason="Fixture operator authorizes this gate", expected_plan_hash=plan_hash, execution_preview=preview),
        current_user=_token_context(actor.org_id, user_id=actor.actor_id),
        access=access,
        db=session,
    )


async def verify_validation_progression(factory, *, flow_id, actor, plan_hash, config, data, monkeypatch):
    observations = cleanup_observations(data, config.repo)
    async with httpx.AsyncClient(transport=observations.transport) as client:
        provider = RepositoryEvidenceProvider(client=client, reviews=SimpleNamespace(bound_pull_request=AsyncMock(return_value=data.review)))
        provider.token = AsyncMock(return_value="fixture-repository-read-token")
        monkeypatch.setattr("src.orchestration.repository_evaluation.RepositoryEvidenceProvider", lambda: provider)
        async with factory() as session:
            nodes = {node.node_ref: node for node in await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow_id))}
            checker, validation, inputs, acceptance = (
                nodes[ref] for ref in ("cleanup-checker", "cleanup-validation", "authorized-inputs", "deploy-gate")
            )
            validation_id, inputs_id, acceptance_id = validation.id, inputs.id, acceptance.id
            receipt = await settle_checker(session, node=checker, config=config)
            observations.responses[f"/repos/{config.repo}/pulls/2"]["merged_at"] = receipt.merged_at.isoformat()
            tick = await run_tick(session)
            assert checker.state == "passed"
            assert tick.blocked[validation_id] == [inputs_id]
            missing = await unresolved_required_inputs(session, validation)
            assert [(item.name, item.source) for item in missing] == [
                ("authorized-cleanup-inputs", "node:demo-flow/epic-1/prerequisites/authorized-inputs")
            ]
            assert (await run_dispatch_pass(session, config)).dispatched == 0
            assert observations.calls == []
            assert validation.state == acceptance.state == "pending"
            approved = await approve_fixture_gate(session, gate=inputs, actor=actor, plan_hash=plan_hash)
            assert approved.state == "passed" and approved.actor_kind == "human"
            await run_tick(session)
            await session.refresh(validation)
            assert validation.state == "ready"
            assert await unresolved_required_inputs(session, validation) == []
            await run_dispatch_pass(session, config)
            await session.refresh(acceptance)
            assert validation.state == "ready" and acceptance.state == "pending"
            missing_workflow = (
                await session.scalars(
                    select(OrchestrationDecision).where(
                        OrchestrationDecision.node_id == validation_id, OrchestrationDecision.actor_id == "system:repository-evaluation"
                    )
                )
            ).one()
            assert json.loads(missing_workflow.reason)["block_code"] == "repository_evaluation_workflow_run_missing"
            await session.commit()

        observations.result(True)
        observations.artifact["digest"] = "sha256:" + "0" * 64
        async with factory() as session:
            await run_dispatch_pass(session, config)
            validation = await session.get(OrchestrationNode, validation_id)
            acceptance = await session.get(OrchestrationNode, acceptance_id)
            assert validation.state == "ready" and validation.attempts == 0 and acceptance.state == "pending"
            rejected = await session.scalar(
                select(OrchestrationDecision)
                .where(OrchestrationDecision.node_id == validation_id, OrchestrationDecision.actor_id == "system:repository-evaluation")
                .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
            )
            assert json.loads(rejected.reason)["block_code"] == "repository_evaluation_artifact_digest"
            await session.commit()

        for passed in (False, True):
            observations.result(passed)
            async with factory() as session:
                report = await run_dispatch_pass(session, config)
                assert report.dispatched == 0 and report.errors == 0 and report.pending == []
                validation = await session.get(OrchestrationNode, validation_id)
                acceptance = await session.get(OrchestrationNode, acceptance_id)
                decisions = list(
                    await session.scalars(
                        select(OrchestrationDecision)
                        .where(OrchestrationDecision.node_id == validation_id, OrchestrationDecision.actor_id == "system:repository-evaluation")
                        .order_by(OrchestrationDecision.created_at, OrchestrationDecision.id)
                    )
                )
                evidence = json.loads(decisions[-1].reason)["receipt"]
                assert evidence["mandatory_passed"] is passed
                assert evidence["accepted_plan_version"] == 2 and evidence["accepted_plan_hash"] == plan_hash
                assert evidence["live_attestation"] is False
                assert evidence["pull_requests"][0]["merge_operation_key"] == receipt.operation_key
                workflow = evidence["workflows"][0]
                assert (workflow["run_id"], workflow["run_attempt"], workflow["source_revision"]) == (10, 2, receipt.merge_sha)
                assert workflow["criteria"] == [{"criterion_id": "cleanup-validation", "passed": passed}]
                assert workflow["artifacts"][0]["digest"] == hashlib.sha256(data.binary).hexdigest()
                assert (validation.state, validation.attempts) == (("passed", 1) if passed else ("ready", 0))
                assert acceptance.state == ("awaiting_gate" if passed else "pending")
                assert len(list(await session.scalars(select(OrchestrationPullRequestBinding)))) == 1
                assert len(list(await session.scalars(select(OrchestrationExecution)))) == 1
                if passed:
                    approved = await approve_fixture_gate(session, gate=acceptance, actor=actor, plan_hash=plan_hash)
                    assert approved.state == "passed" and approved.actor_kind == "human"
                await session.commit()
