"""External verification closes only the exited assignment and retains its failure."""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

import tests.orchestration.test_operations_release_resolution as fixtures
from src.orchestration import external_completion as completion
from src.orchestration.accepted_node import accepted_node_contract
from src.orchestration.compile import ApprovalContext
from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.state import ActorKind, NodeState, transition

engine = fixtures.engine
session = fixtures.session
governed_work_ownership = fixtures.governed_work_ownership
healthy_policy_reservations = fixtures.healthy_policy_reservations
policy_budget_initializers = fixtures.policy_budget_initializers


async def arrange(session, monkeypatch, *, successor=False, criterion_ids=("AC-1", "AC-2")):
    children = []

    async def criteria(flow, _node, plan, _manifest, _provider):
        document = copy.deepcopy(plan.plan_document)
        document["nodes"][-1]["criteria"] = list(criterion_ids)
        if successor:
            child = await fixtures._make_node(session, flow, node_ref="next-operation", state="pending", issue_ref="4197")
            await fixtures._make_usage(session, child)
            children.append(child)
            parent = document["nodes"][-1]
            child_spec = copy.deepcopy(parent)
            child_spec["address"] = f"{flow.slug}/{child.epic_ref}/{child.wave_ref}/{child.node_ref}"
            child_spec["operational_release_binding"]["predecessors"].append(
                {"address": parent["address"], "receipt_kind": "verified_operations_release"}
            )
            child_spec["required_inputs"].append({"name": "verified_release", "source": f"node:{parent['address']}"})
            document["nodes"].append(child_spec)
            document["edges"].append({"source": parent["address"], "target": child_spec["address"]})
        plan.plan_document = document
        plan.plan_hash = completion.digest(document)

    fixture = await fixtures._dispatched_release(session, monkeypatch, prepare_release=criteria)
    fixture.successor = children[0] if children else None
    monkeypatch.setattr("src.orchestration.operations_release_predecessor.load_packaged_manifest", lambda: fixture.manifest)
    node, plan = fixture.node, fixture.plan
    execution = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))
    claim = await session.get(OrchestrationWorkClaim, execution.claim_id)
    run_id = attempt_run_id(node.id, node.attempts)
    metadata = fixture.first_pass.pending[0].envelope
    report = await session.get(OrchestrationRunReport, run_id)
    if report is None:
        report = OrchestrationRunReport(
            run_id=run_id,
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            attempt=node.attempts,
            persona="operations",
            repo=fixtures.REPO,
            installation_id=7,
            provider_repository_id=12345,
            dispatch_metadata=metadata,
            credential_hash="e" * 64,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        session.add(report)
    terminal = {"outcome": "failed", "run_id": run_id, "attempt": node.attempts, "failure": {"category": "deadline"}}
    report.terminal_receipt = terminal
    node.state = "ready"
    claim.state, claim.release_reason, claim.active_run_id, claim.claim_event_id = "released", "failed", None, run_id
    await session.flush()
    request = completion.ExternalCompletionRequest(
        node_id=node.id,
        expected_attempt=node.attempts,
        expected_plan_version=plan.version,
        expected_plan_hash=plan.plan_hash,
        workflow_run_id=99,
        workflow_run_attempt=1,
        source_revision=fixtures.SOURCE,
        workflow_revision=fixtures.DEFINITION,
        criteria=[{"criterion_id": c, "job_id": 11, "evidence_ref": f"https://example.invalid/reports/{c}"} for c in criterion_ids],
        required_job_ids=[11, 12],
        release_evidence_ref="https://example.invalid/release",
        summary="Externally reviewed API cases and fixture cleanup passed.",
        reason="Owner requested acceptance of separately verified work.",
    )
    run = {
        "id": 99,
        "run_attempt": 1,
        "status": "completed",
        "conclusion": "failure",
        "head_sha": fixtures.DEFINITION,
        "repository": {"id": report.provider_repository_id},
        "path": ".github/workflows/gateway-deploy.yml",
        "html_url": "https://example.invalid/run/99",
    }
    jobs = [
        {
            "id": i,
            "run_id": 99,
            "run_attempt": 1,
            "head_sha": fixtures.DEFINITION,
            "name": f"job-{i}",
            "status": "completed",
            "conclusion": "failure" if i == 13 else "success",
            "html_url": f"https://example.invalid/jobs/{i}",
        }
        for i in [11, 12, 13]
    ]
    provider = SimpleNamespace(request=AsyncMock(return_value=SimpleNamespace(json=lambda: run)), pages=AsyncMock(return_value=jobs))
    actor = ApprovalContext(org_id=node.org_id, actor_id="owner", actor_role="platform_admin", reason=request.reason)
    return fixture, execution, claim, report, request, actor, provider, run, jobs


@pytest.mark.parametrize(
    "criterion_ids",
    [
        ("AC-1", "AC-2"),
        (),
        (
            "Use the merged workflow definition and the exact reviewed saved registration plan in the accepted AWS account and region. "
            "Do not replay coordinator-applied prerequisite creates.",
            "Verify the product group and project membership, isolated qualification groups, least-scope tokens, revocation and trusted CA. "
            "Attach genuine workflow evidence before concluding the story.",
        ),
    ],
)
async def test_external_acceptance_retains_failed_attempt_and_provides_lineage(session, monkeypatch, governed_work_ownership, criterion_ids):
    fixture, execution, claim, report, request, actor, provider, _, _ = await arrange(session, monkeypatch, criterion_ids=criterion_ids)
    old_terminal = copy.deepcopy(report.terminal_receipt)
    preview = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
    assert fixture.node.state == "ready" and not preview["accepted"]
    assert preview["proof"]["workflow_conclusion"] == "failure"
    assert [j["id"] for j in preview["proof"]["other_failed_jobs"]] == [13]
    request = request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    result = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider, accept=True)
    assert result["accepted"] and fixture.node.state == "passed"
    await session.refresh(execution)
    assert execution.status == execution.phase == "concluded"
    assert report.terminal_receipt == old_terminal and claim.release_reason == "failed"
    assert fixture.node.attempts == request.expected_attempt
    accepted = await accepted_node_contract(session, fixture.node)
    source = await completion.external_release_source(session, fixture.node, accepted, fixture.plan)
    assert source.revision == fixtures.SOURCE and source.provenance["authority"] == "owner_acceptance_of_external_evidence"
    assert source.provenance["address"] == accepted.node.address
    repeated = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider, accept=True)
    assert repeated["created"] is False and repeated["decision_id"] == result["decision_id"]
    assert len(list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == completion.KIND)))) == 1


async def test_runtime_only_completion_still_requires_successful_jobs(session, monkeypatch, governed_work_ownership):
    fixture, _, _, _, request, actor, provider, _, jobs = await arrange(session, monkeypatch, criterion_ids=())
    jobs[0]["conclusion"] = "skipped"
    with pytest.raises(completion.ExternalCompletionError, match="required_jobs_not_successful"):
        await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
    assert fixture.node.state == "ready"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("usage_recorded", [False, True])
async def test_external_completion_successor_dispatch_survives_restart(session, monkeypatch, governed_work_ownership, legacy, usage_recorded):
    from src.budget.config import budget_config
    from src.orchestration.dispatch_pass import run_dispatch_pass
    from src.orchestration.operations_release_resolution import recorded_operations_release

    # Leave room for both admissions: the unmeasured parent's hold stays intact.
    monkeypatch.setattr(budget_config, "budget_run_cap_usd", Decimal("10"))
    fixture, _, _, _, request, actor, provider, _, _ = await arrange(session, monkeypatch, successor=True)
    preview = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
    request = request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider, accept=True)
    if usage_recorded:
        await fixtures._make_usage(session, fixture.node)
    child = fixture.successor
    child.state = "ready"
    source_reader = completion.external_release_source
    if legacy:

        async def legacy_source(*args, **kwargs):
            source = await source_reader(*args, **kwargs)
            return replace(source, provenance={k: v for k, v in source.provenance.items() if k != "address"}) if source else None

        monkeypatch.setattr(completion, "external_release_source", legacy_source)
    first = await run_dispatch_pass(session, fixtures._config())
    monkeypatch.setattr(completion, "external_release_source", source_reader)
    assert first.dispatched == len(first.pending) == 1
    execution = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == child.id))
    decision = await session.scalar(
        select(OrchestrationDecision).where(OrchestrationDecision.node_id == child.id, OrchestrationDecision.kind == "node_dispatched")
    )
    dispatch = json.loads(decision.reason)
    original = copy.deepcopy(dispatch["dispatch_envelope"])
    original_transport = copy.deepcopy(first.pending[0].envelope)
    assert "run_report" in original_transport
    assert original == {key: value for key, value in original_transport.items() if key != "run_report"}
    await session.commit()
    monkeypatch.setattr(
        "src.agentauth.engine.get_engine_authority_writer", lambda: SimpleNamespace(store=SimpleNamespace(_read=Mock(return_value=None)))
    )
    fixture.provider.definition.side_effect = AssertionError("Replay must retain the admitted source")
    fixture.provider.contains.side_effect = AssertionError("Replay must retain the admitted ancestry")
    recovered = await run_dispatch_pass(session, fixtures._config())
    assert recovered.publish_failed == 0 and len(recovered.pending) == 1
    assert recovered.pending[0].envelope == original_transport
    assert recovered.pending[0].deduplication_id == first.pending[0].deduplication_id
    assert child.attempts == 1
    assert json.loads(decision.reason)["dispatch_envelope"] == original
    contract = await accepted_node_contract(session, child)
    assert (
        await recorded_operations_release(session, node=child, execution=execution, contract=contract, metadata=original["orchestration"])
        == original["orchestration"]["deployment_revisions"]
    )


@pytest.mark.parametrize("meter_state", ["pending", "retained", "settled", "missing", "unknown", "sql_larger"])
async def test_external_completion_uses_existing_meter_without_releasing_holds(session, monkeypatch, governed_work_ownership, meter_state):
    from src.orchestration.flow_budget import admission_request_id, flow_reservation_target, get_flow_reservations
    from src.orchestration.flow_meter import meter_target
    from src.orchestration.policy_admission import _observed_spend, load_in_force_policy

    fixture, _, _, report, request, actor, provider, _, _ = await arrange(session, monkeypatch)
    preview = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
    await completion.complete_external(
        session,
        flow_id=fixture.flow.id,
        actor=actor,
        request=request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
        provider=provider,
        accept=True,
    )
    policy = (await load_in_force_policy(session, org_id=fixture.flow.org_id, flow_id=fixture.flow.id)).policy
    nodes = list(await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == fixture.flow.id)))
    store = get_flow_reservations()
    target = meter_target(org_id=fixture.flow.org_id, flow_id=fixture.flow.id, policy=policy)
    client = await store._get_client()
    holds = flow_reservation_target(org_id=fixture.flow.org_id, flow_id=fixture.flow.id, policy=policy, settled_usd=Decimal(0))
    prior_hold = await client.hget(holds.key(), admission_request_id(fixture.node.id))
    assert prior_hold is not None
    if meter_state == "missing":
        await client.delete(target.key())
    elif meter_state != "sql_larger":
        assert (await store.reserve("prior-call", Decimal("11"), [target])).admitted
        if meter_state == "retained":
            assert await store.retain_failed_bound("prior-call", target)
        elif meter_state == "settled":
            await store.reconcile("prior-call", Decimal("11"), [target])
        elif meter_state == "unknown":
            await store.mark_unknown("prior-call", target)
    observed = await _observed_spend(session, org_id=fixture.flow.org_id, flow_slug=fixture.flow.slug, nodes=nodes, policy=policy)
    if meter_state in {"missing", "unknown"}:
        assert observed.total_usd is None
    else:
        assert observed.total_usd == Decimal("2" if meter_state == "sql_larger" else "11")
    assert fixture.node.id not in observed.settled_node_ids
    assert await client.hget(holds.key(), admission_request_id(fixture.node.id)) == prior_hold
    assert report.terminal_receipt["outcome"] == "failed"


@pytest.mark.parametrize("invalid", ["missing", "tenant", "actor", "attempt", "execution", "malformed"])
async def test_external_completion_spend_requires_current_trusted_acceptance(session, monkeypatch, governed_work_ownership, invalid):
    from src.orchestration.policy_admission import _observed_spend, load_in_force_policy

    fixture, execution, _, _, request, actor, provider, _, _ = await arrange(session, monkeypatch)
    preview = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
    result = await completion.complete_external(
        session,
        flow_id=fixture.flow.id,
        actor=actor,
        request=request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
        provider=provider,
        accept=True,
    )
    decision = await session.get(OrchestrationDecision, result["decision_id"])
    if invalid == "missing":
        await session.delete(decision)
    elif invalid in {"tenant", "actor", "malformed"}:
        # Seed a separate invalid record; normal decisions are append-only.
        values = {column.name: getattr(decision, column.name) for column in OrchestrationDecision.__table__.columns if column.name != "id"}
        values.update({"tenant": {"org_id": "another-tenant"}, "actor": {"actor_kind": "service"}, "malformed": {"reason": "{"}}[invalid])
        await session.delete(decision)
        await session.flush()
        session.add(OrchestrationDecision(**values))
    elif invalid == "attempt":
        fixture.node.attempts += 1
    elif invalid == "execution":
        execution.status = "active"
    await session.flush()
    policy = (await load_in_force_policy(session, org_id=fixture.flow.org_id, flow_id=fixture.flow.id)).policy
    observed = await _observed_spend(session, org_id=fixture.flow.org_id, flow_slug=fixture.flow.slug, nodes=[fixture.node], policy=policy)
    assert observed.total_usd is None


@pytest.mark.parametrize("invalid", ["decision", "source", "execution", "flow", "non_acceptance", "parent_state", "extra_field", "repository"])
async def test_legacy_external_receipt_requires_exact_trusted_acceptance(session, monkeypatch, governed_work_ownership, invalid):
    fixture, _, _, _, request, actor, provider, _, _ = await arrange(session, monkeypatch)
    preview = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
    request = request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider, accept=True)
    accepted = await accepted_node_contract(session, fixture.node)
    source = await completion.external_release_source(session, fixture.node, accepted, fixture.plan)
    receipt = {k: v for k, v in source.provenance.items() if k != "address"}
    context, repository = fixture.node, fixtures.REPO
    if invalid == "decision":
        receipt["external_completion_decision_id"] = "unknown"
    elif invalid == "source":
        receipt["source_revision"] = "f" * 40
    elif invalid == "execution":
        receipt["execution_id"] = "another-execution"
    elif invalid == "flow":
        context = SimpleNamespace(org_id=fixture.node.org_id, flow_id="another-flow")
    elif invalid == "non_acceptance":
        receipt["external_completion_decision_id"] = fixture.plan.accepted_by_decision_id
    elif invalid == "parent_state":
        fixture.node.state = "running"
    elif invalid == "extra_field":
        receipt["unexpected"] = "unverified"
    elif invalid == "repository":
        repository = "another/repository"
    await session.flush()
    assert await completion.external_receipt_address(session, node=context, receipt=receipt, repository=repository) is None


@pytest.mark.parametrize("invalid", ["active_worker", "source", "criterion", "job", "repository", "attempt", "outstanding", "snapshot", "tenant"])
async def test_external_acceptance_rejects_unverified_or_changed_work(session, monkeypatch, governed_work_ownership, invalid):
    fixture, execution, claim, report, request, actor, provider, run, jobs = await arrange(session, monkeypatch)
    if invalid == "active_worker":
        claim.state = "held"
    elif invalid == "source":
        request = request.model_copy(update={"source_revision": "f" * 40})
    elif invalid == "criterion":
        request = request.model_copy(update={"criteria": request.criteria[:1]})
    elif invalid == "job":
        jobs[0]["conclusion"] = "failure"
    elif invalid == "repository":
        run["repository"]["id"] += 1
    elif invalid == "attempt":
        request = request.model_copy(update={"expected_attempt": request.expected_attempt + 1})
    elif invalid == "outstanding":
        session.add(
            OrchestrationAction(
                org_id=fixture.node.org_id,
                execution_id=execution.id,
                operation_key="unfinished",
                kind="external-effect",
                status="unknown",
                attempt=1,
                detail={},
            )
        )
    elif invalid == "tenant":
        actor = ApprovalContext(org_id="another-tenant", actor_id="owner", actor_role="platform_admin", reason=request.reason)
    await session.flush()
    with pytest.raises(completion.ExternalCompletionError):
        await completion.complete_external(
            session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider, accept=invalid == "snapshot"
        )
    assert fixture.node.state == "ready" and report.terminal_receipt["outcome"] == "failed"
    assert not await session.scalar(select(OrchestrationDecision).where(OrchestrationDecision.kind == completion.KIND))


async def test_changed_execution_after_preview_cannot_be_completed(session, monkeypatch, governed_work_ownership):
    fixture, execution, _, report, request, actor, provider, _, _ = await arrange(session, monkeypatch)
    preview = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
    execution.revision += 1
    await session.flush()
    with pytest.raises(completion.ExternalCompletionError, match="completion_snapshot_changed"):
        await completion.complete_external(
            session,
            flow_id=fixture.flow.id,
            actor=actor,
            request=request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
            provider=provider,
            accept=True,
        )
    assert fixture.node.state == "ready" and report.terminal_receipt["outcome"] == "failed"


@pytest.mark.parametrize("identical", [True, False])
async def test_newer_dispatch_head_requires_identical_reviewed_workflow(session, monkeypatch, governed_work_ownership, identical):
    fixture, _, _, _, request, actor, provider, run, jobs = await arrange(session, monkeypatch)
    request = request.model_copy(update={"workflow_revision": "e" * 40})
    run.update(event="workflow_dispatch", head_sha=request.workflow_revision)
    for job in jobs:
        job["head_sha"] = request.workflow_revision
    provider.contains = AsyncMock()
    provider.workflow_content_matches = AsyncMock(return_value=identical)
    if identical:
        preview = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
        assert preview["proof"]["workflow_revision"] == request.workflow_revision
        assert preview["proof"]["approved_definition_revision"] == fixtures.DEFINITION
    else:
        with pytest.raises(completion.ExternalCompletionError, match="release_workflow_changed"):
            await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
    assert provider.contains.call_args.args[1:] == (fixtures.SOURCE, request.workflow_revision)
    assert provider.workflow_content_matches.call_args.args[2:] == (fixtures.DEFINITION, request.workflow_revision)


@pytest.mark.parametrize("changed", ["repository", "installation", "source", "criteria", "execution"])
async def test_external_lineage_rejects_changed_identity(session, monkeypatch, governed_work_ownership, changed):
    fixture, execution, _, _, request, actor, provider, _, _ = await arrange(session, monkeypatch)
    preview = await completion.complete_external(session, flow_id=fixture.flow.id, actor=actor, request=request, provider=provider)
    result = await completion.complete_external(
        session,
        flow_id=fixture.flow.id,
        actor=actor,
        request=request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
        provider=provider,
        accept=True,
    )
    decision = await session.get(OrchestrationDecision, result["decision_id"])
    content = json.loads(decision.reason)
    if changed in {"repository", "installation"}:
        content["proof"][changed + "_id"] += 1
    elif changed == "source":
        content["proof"]["source_revision"] = "f" * 40
    elif changed == "criteria":
        content["request"]["criteria"] = content["request"]["criteria"][:1]
    else:
        execution.status = "awaiting_external"
    if changed != "execution":
        session.add(
            OrchestrationDecision(
                org_id=decision.org_id,
                flow_id=decision.flow_id,
                node_id=decision.node_id,
                kind=decision.kind,
                actor_id=decision.actor_id,
                actor_role=decision.actor_role,
                actor_kind=decision.actor_kind,
                to_state=decision.to_state,
                reason=json.dumps(content),
                created_at=decision.created_at + timedelta(seconds=1),
            )
        )
    await session.flush()
    accepted = await accepted_node_contract(session, fixture.node)
    with pytest.raises(completion.ExternalCompletionError):
        await completion.external_release_source(session, fixture.node, accepted, fixture.plan, repository_id=12345, installation_id=7)


@pytest.mark.parametrize("state", [NodeState.READY, NodeState.FAILED])
def test_only_verified_owner_completion_can_take_the_recovery_edge(state):
    assert not transition(state, NodeState.PASSED, actor_kind=ActorKind.HUMAN, reason="unverified").allowed
    assert not transition(state, NodeState.PASSED, actor_kind=ActorKind.SERVICE, reason="unverified", external_completion_authorized=True).allowed
    assert transition(state, NodeState.PASSED, actor_kind=ActorKind.HUMAN, reason="verified", external_completion_authorized=True).allowed
