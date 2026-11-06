"""Registered draft edits retain their gate, history and inert authority (#5331)."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.admin.config import AdminRole, Permission
from src.auth.dependencies import get_current_user
from src.orchestration import draft_revision as revision
from src.orchestration.compile import ApprovalContext
from src.orchestration.draft_revision_routes import get_access_control, router
from src.orchestration.genesis import APPROVAL_DECISION_KINDS
from src.orchestration.models import OrchestrationFlow
from src.orchestration.proposal import NodeLineage, ProposedEdge
from src.orchestration.registration import register_draft_proposal
from src.orchestration.repository import OrchestrationRepository
from src.orchestration.state import ActorKind, NodeState
from src.shared.database import get_db
from tests.orchestration import test_registration as reg

session = reg.session
registrar = reg.registrar
access = reg.access
autonomy_default_unset = reg.autonomy_default_unset
provider_repository_identity = reg.provider_repository_identity


@pytest.fixture
def operator():
    return ApprovalContext(org_id=reg.ORG_A, actor_id=reg.HUMAN_USER_ID, actor_role="org_admin", actor_kind=ActorKind.HUMAN)


@pytest.fixture
async def draft(session, registrar):
    await reg.seed_org(session)
    authored = reg.gateless_proposal()
    result, _ = await register_draft_proposal(session, authored, registrar)
    await session.commit()
    return result, authored


def request_for(draft, **updates):
    result, authored = draft
    # Moving one story leaves its old identity in history and exercises edge removal.
    nodes = [n.model_copy(update={"address": n.address.replace("/story-b", "/story-new")}) for n in authored.nodes]
    edges = [ProposedEdge(from_address=e.from_address.replace("/story-b", "/story-new"), to_address=e.to_address) for e in authored.edges]
    payload = dict(
        proposal=authored.model_copy(update={"nodes": nodes, "edges": edges, "title": "Revised draft"}),
        expected_plan_version=result.plan_version,
        expected_plan_hash=result.plan_hash,
        reason="Review the wave structure before approval.",
    )
    payload.update(updates)
    return revision.DraftRevisionRequest(**payload)


async def save(session, draft, operator, request=None):
    request = request or request_for(draft)
    preview = await revision.preview_draft_revision(session, draft[0].flow_id, request, operator)
    write = revision.SaveDraftRevisionRequest(**request.model_dump(), expected_proposal_hash=preview["proposal_hash"])
    return await revision.save_draft_revision(session, draft[0].flow_id, write, operator), write


async def records(session, flow_id):
    repo = OrchestrationRepository(session)
    return await repo.list_plan_versions(org_id=reg.ORG_A, flow_id=flow_id), await repo.list_decisions(org_id=reg.ORG_A, flow_id=flow_id)


async def test_operations_draft_can_be_saved_before_activation(session, draft, operator, monkeypatch):
    from src.orchestration.executor_assignment import selected_executor

    monkeypatch.delenv("ADP_ENGINE_OPERATIONS_EXECUTOR", raising=False)
    request = request_for(draft).model_dump(mode="json")
    node = next(n for n in request["proposal"]["nodes"] if n["kind"] == "story")
    node["executor"] = {"kind": "agent", "role": "operate", "persona": "agent-codex-operations"}
    node["evidence_expectations"] = ["runtime_verified:tools-facade"]
    node["required_inputs"] = [
        {"name": name, "source": source}
        for name, source in (
            ("workflow", "deployment_workflow:tools-facade-deploy.yml"),
            ("source", "deployment_source_revision:" + "a" * 40),
            ("definition", "deployment_definition_revision:" + "b" * 40),
            ("branch", "deployment_branch:main"),
        )
    ]
    result, _ = await save(session, draft, operator, revision.DraftRevisionRequest.model_validate(request))
    assert result["execution_authorized"] is False
    assert result["proposal"]["execution_policy"] is None
    stored = next(n for n in result["proposal"]["nodes"] if n["address"] == node["address"])
    assert stored["required_inputs"] == [{**i, "description": None} for i in node["required_inputs"]]
    with pytest.raises(ValueError, match="unsupported_node_executor"):
        selected_executor(result["proposal"], node["address"], default="developer", accepted=True)
    assert (await reg.nodes_by_ref(session))["accept"].state == NodeState.AWAITING_GATE.value


@pytest.mark.parametrize(
    "source",
    ["deployment_workflow:../gateway.yml", "deployment_source_revision:main", "deployment_definition_revision:latest", "deployment_branch:main/path"],
)
def test_operations_draft_refuses_malformed_workflow_inputs(monkeypatch, source):
    from src.orchestration.proposal import LoopProposal, validate_proposal

    monkeypatch.delenv("ADP_ENGINE_OPERATIONS_EXECUTOR", raising=False)
    raw = reg.gateless_proposal().model_dump(mode="json")
    node = next(n for n in raw["nodes"] if n["kind"] == "story")
    node["executor"] = {"kind": "agent", "role": "operate", "persona": "operations"}
    node["required_inputs"] = [{"name": "invalid", "source": source}]
    assert any(v.rule == "required_input_source_unsupported" for v in validate_proposal(LoopProposal.model_validate(raw)))


def _release_proposal():
    from src.orchestration.proposal import LoopProposal

    raw = reg.gateless_proposal().model_dump(mode="json")
    node = next(item for item in raw["nodes"] if item["address"] == reg.address("story-c", wave="wave-2"))
    predecessor = reg.address("story-a")
    node["executor"] = {"kind": "agent", "role": "operate", "persona": "operations"}
    node["repository_identity"] = "aws-e/adp"
    node["required_inputs"] = [
        {"name": "build", "source": f"node:{predecessor}"},
        {"name": "workflow", "source": "deployment_workflow:gateway-deploy.yml"},
    ]
    node["operational_release_binding"] = {
        "predecessors": [{"address": predecessor, "receipt_kind": "verified_merge"}],
        "build_prerequisites": [predecessor],
        "repository": "aws-e/adp",
        "target": "gateway-dev",
        "component": "gateway",
        "workflows": ["gateway-deploy.yml"],
        "default_branch": "main",
    }
    return LoopProposal.model_validate(raw)


def test_future_release_binding_round_trips_without_changing_literal_nodes():
    from src.orchestration.proposal import LoopProposal, validate_proposal

    proposal = _release_proposal()
    serialized = proposal.model_dump(mode="json")
    assert "operational_release_binding" not in serialized["nodes"][0]
    assert LoopProposal.model_validate(serialized).model_dump(mode="json") == serialized
    assert not any(violation.rule == "operational_release_binding_invalid" for violation in validate_proposal(proposal))


@pytest.mark.parametrize(
    "component,workflow,selected",
    [
        ("gateway-backend", "gateway-deploy.yml", "api_product_lifecycle,api_denial_oracle,api_recovery_cleanup"),
        ("gateway-frontend", "gateway-frontend-deploy.yml", "live_browser_create,live_browser_conflict_denial,live_browser_evidence"),
        ("gateway-frontend", "gateway-frontend-deploy.yml", "live_scope_isolation,live_scope_denials,live_scope_history_cleanup"),
    ],
)
def test_product_inventory_requires_reviewed_gateway_deployment_binding(component, workflow, selected):
    from src.orchestration.proposal import LoopProposal

    raw = _release_proposal().model_dump(mode="json")
    node = next(item for item in raw["nodes"] if item.get("operational_release_binding"))
    binding = node["operational_release_binding"]
    assert "product_cases" not in binding
    binding.update(component=component, workflows=[workflow], product_cases=selected)
    accepted = LoopProposal.model_validate(raw)
    persisted = next(item for item in accepted.model_dump(mode="json")["nodes"] if item.get("operational_release_binding"))
    assert persisted["operational_release_binding"]["product_cases"] == selected
    for field, value in (("component", "gateway"), ("workflows", ["other.yml"]), ("product_cases", "api_product_lifecycle")):
        invalid = {**binding, field: value}
        node["operational_release_binding"] = invalid
        with pytest.raises(ValueError):
            LoopProposal.model_validate(raw)


async def test_future_release_binding_is_stored_without_revisions(session, draft, operator):
    from src.orchestration.accepted_node import AcceptedNodeContract, operational_revision_inputs
    from src.orchestration.proposal import LoopProposal

    request = request_for(draft).model_dump(mode="json")
    node = next(item for item in request["proposal"]["nodes"] if item["address"].endswith("/story-c"))
    release = next(item for item in _release_proposal().model_dump(mode="json")["nodes"] if item.get("operational_release_binding"))
    node.update(
        executor=release["executor"],
        repository_identity=release["repository_identity"],
        required_inputs=release["required_inputs"],
        operational_release_binding=release["operational_release_binding"],
    )
    result, _ = await save(session, draft, operator, revision.DraftRevisionRequest.model_validate(request))
    stored = next(item for item in result["proposal"]["nodes"] if item["address"] == node["address"])
    assert stored["operational_release_binding"] == release["operational_release_binding"]
    accepted = LoopProposal.model_validate(result["proposal"])
    operating = next(item for item in accepted.nodes if item.address == node["address"])
    assert operational_revision_inputs(AcceptedNodeContract(1, operating, frozenset(), frozenset())) is None


@pytest.mark.parametrize("invalid", ["literal", "foreign_predecessor", "wrong_repository", "wrong_workflow"])
def test_future_release_binding_rejects_inconsistent_authority(invalid):
    from src.orchestration.proposal import LoopProposal, validate_proposal

    raw = _release_proposal().model_dump(mode="json")
    node = next(item for item in raw["nodes"] if item.get("operational_release_binding"))
    binding = node["operational_release_binding"]
    if invalid == "literal":
        node["required_inputs"].append({"name": "source", "source": "deployment_source_revision:" + "a" * 40})
    elif invalid == "foreign_predecessor":
        binding["predecessors"][0]["address"] = reg.address("story-b")
        binding["build_prerequisites"] = [reg.address("story-b")]
    elif invalid == "wrong_repository":
        binding["repository"] = "another/repository"
    else:
        binding["workflows"] = ["other-deploy.yml"]
    assert any(violation.rule == "operational_release_binding_invalid" for violation in validate_proposal(LoopProposal.model_validate(raw)))


async def test_preview_writes_nothing(session, draft, operator):
    before = await reg.nodes_by_ref(session)
    preview = await revision.preview_draft_revision(session, draft[0].flow_id, request_for(draft), operator)
    assert preview["wrote_nothing"] and not preview["execution_authorized"]
    assert preview["added_nodes"] == [reg.address("story-new")]
    assert preview["removed_nodes"] == [reg.address("story-b")]
    assert len((await records(session, draft[0].flow_id))[0]) == 1
    assert {k: n.id for k, n in (await reg.nodes_by_ref(session)).items()} == {k: n.id for k, n in before.items()}


@pytest.mark.parametrize("paused", [False, True])
async def test_save_preserves_gate_pause_and_inertness(session, draft, operator, paused):
    from src.orchestration.dispatch_pass import run_dispatch_pass
    from src.orchestration.tick import run_tick

    flow_id = draft[0].flow_id
    flow = await session.get(OrchestrationFlow, flow_id)
    flow.execution_paused = paused
    before = await reg.nodes_by_ref(session)
    gate_id = before["accept"].id
    result, _ = await save(session, draft, operator)
    await session.commit()
    assert result["plan_version"] == 2 and result["execution_paused"] is paused
    nodes = await reg.nodes_by_ref(session)
    assert nodes["accept"].id == gate_id and nodes["accept"].state == NodeState.AWAITING_GATE.value
    assert nodes["story-a"].id == before["story-a"].id
    assert nodes["story-b"].state == NodeState.SUPERSEDED.value
    assert nodes["story-new"].state == NodeState.PENDING.value
    plans, decisions = await records(session, flow_id)
    assert len(plans) == 2 and plans[0].superseded_at is not None
    assert plans[0].plan_hash == draft[0].plan_hash
    assert all(d.kind not in APPROVAL_DECISION_KINDS for d in decisions)
    for _ in range(3):
        await run_tick(session)
        report = await run_dispatch_pass(session, reg.dispatch_config())
        assert report.dispatched == 0 and report.pending == []
    assert all(n.attempts == 0 for n in nodes.values())
    assert flow.execution_paused is paused


async def test_response_lost_retry_is_idempotent(session, draft, operator):
    first, request = await save(session, draft, operator)
    await session.commit()
    replay = await revision.save_draft_revision(session, draft[0].flow_id, request, operator)
    assert replay["already_revised"] and replay["plan_version"] == first["plan_version"]
    assert replay["plan_hash"] == first["plan_hash"]
    plans, decisions = await records(session, draft[0].flow_id)
    assert len(plans) == len(decisions) == 2


async def test_metadata_only_edit_is_saved_and_bound_to_preview(session, draft, operator):
    request = request_for(draft, proposal=draft[1].model_copy(update={"description": "Clarified intent"}))
    preview = await revision.preview_draft_revision(session, draft[0].flow_id, request, operator)
    changed = request.model_copy(update={"proposal": request.proposal.model_copy(update={"description": "Unreviewed intent"})})
    with pytest.raises(revision.DraftRevisionConflictError, match="reviewed preview"):
        await revision.save_draft_revision(
            session,
            draft[0].flow_id,
            revision.SaveDraftRevisionRequest(**changed.model_dump(), expected_proposal_hash=preview["proposal_hash"]),
            operator,
        )
    result, _ = await save(session, draft, operator, request)
    assert result["plan_version"] == 2 and result["plan_hash"] == draft[0].plan_hash
    assert (await session.get(OrchestrationFlow, draft[0].flow_id)).description == "Clarified intent"


@pytest.mark.parametrize(
    ("lineage", "labels"),
    [(NodeLineage(story_id="EI-S02", size="M"), {}), (None, {"source": "issue"}), (NodeLineage(story_id="EI-S02"), {"source": "issue"})],
)
async def test_lineage_and_labels_only_revision_changes_hash_and_preserves_gate(session, draft, operator, lineage, labels):
    before = await reg.nodes_by_ref(session)
    proposal = draft[1]
    story = proposal.nodes[0].model_copy(update={"lineage": lineage, "labels": labels})
    proposal = proposal.model_copy(update={"nodes": [story, *proposal.nodes[1:]]})
    request = request_for(draft, proposal=proposal)
    preview = await revision.preview_draft_revision(session, draft[0].flow_id, request, operator)
    assert preview["proposal_hash"] != revision.draft_hash(draft[1])
    assert preview["proposal"]["nodes"][1].get("lineage", {}).get("story_id") == (lineage.story_id if lineage else None)
    assert preview["acceptance_gate_id"] == before["accept"].id
    result, _ = await save(session, draft, operator, request)
    await session.commit()
    assert result["plan_version"] == 2
    assert result["plan_hash"] != draft[0].plan_hash
    assert (await reg.nodes_by_ref(session))["accept"].id == before["accept"].id
    plans, _ = await records(session, draft[0].flow_id)
    revised = next(plan for plan in plans if plan.version == 2)
    saved = next(node for node in revised.plan_document["nodes"] if node["address"] == story.address)
    assert saved.get("lineage", {}).get("story_id") == (lineage.story_id if lineage else None)
    assert saved.get("labels", {}) == labels
    stored = (await reg.nodes_by_ref(session))[story.address.rsplit("/", 1)[-1]]
    assert stored.lineage == saved.get("lineage")
    assert stored.labels == labels


async def test_invalid_revision_lineage_and_labels_are_rejected_before_save(session, draft, operator):
    proposal = draft[1]
    story = proposal.nodes[0].model_copy(update={"lineage": NodeLineage(story_id="  "), "labels": {"unregistered": "value"}})
    proposal = proposal.model_copy(update={"nodes": [story, *proposal.nodes[1:]]})
    with pytest.raises(revision.ProposalRejectedError) as error:
        await revision.preview_draft_revision(session, draft[0].flow_id, request_for(draft, proposal=proposal), operator)
    assert {violation.rule for violation in error.value.violations} >= {"lineage_invalid", "label_key_unknown"}
    assert len((await records(session, draft[0].flow_id))[0]) == 1


async def test_retry_is_bound_to_actor_and_base_hash(session, draft, operator):
    _, request = await save(session, draft, operator)
    await session.commit()
    other = ApprovalContext(org_id=operator.org_id, actor_id="another-human", actor_role=operator.actor_role)
    with pytest.raises(revision.DraftRevisionConflictError, match="current draft changed"):
        await revision.save_draft_revision(session, draft[0].flow_id, request, other)
    with pytest.raises(revision.DraftRevisionConflictError, match="current draft changed"):
        await revision.save_draft_revision(session, draft[0].flow_id, request.model_copy(update={"expected_plan_hash": "0" * 64}), operator)
    assert len((await records(session, draft[0].flow_id))[0]) == 2


@pytest.mark.parametrize("field", ["execution_policy", "proposed_execution_policy"])
async def test_policy_is_retained_as_proposed_and_cannot_be_removed(session, draft, operator, field):
    from src.orchestration.execution_policy import ExecutionPolicy

    policy = ExecutionPolicy.model_validate(
        {
            "org_id": reg.ORG_A,
            "repository_ids": ["42"],
            "allowed_actions": ["develop"],
            "expires_at": "2999-01-01T00:00:00Z",
            "limits": {"max_wall_clock_seconds": 600, "max_spend_usd": "5", "max_attempts_per_node": 1, "max_concurrent_actions": 1},
        }
    )
    request = request_for(draft)
    request = request.model_copy(update={"proposal": request.proposal.model_copy(update={field: policy})})
    result, _ = await save(session, draft, operator, request)
    stored = (await records(session, draft[0].flow_id))[0][-1].plan_document
    assert stored["execution_policy"] is None
    assert stored["proposed_execution_policy"] == policy.model_dump(mode="json")
    removal = request_for(draft, expected_plan_version=2, expected_plan_hash=result["plan_hash"])
    with pytest.raises(revision.DraftRevisionConflictError, match="Retain proposed policy"):
        await revision.preview_draft_revision(session, draft[0].flow_id, removal, operator)


@pytest.mark.parametrize("mutation", ["tenant", "cycle", "runtime_eval"])
async def test_invalid_proposal_is_refused_without_writes(session, draft, operator, mutation):
    request = request_for(draft)
    if mutation == "tenant":
        proposal = request.proposal.model_copy(update={"org_id": "other"})
    elif mutation == "cycle":
        proposal = request.proposal.model_copy(
            update={"edges": [*request.proposal.edges, ProposedEdge(from_address=reg.address("eval-w1"), to_address=reg.address("story-a"))]}
        )
    else:
        # Even a runtime specification supplied in a draft must be refused.
        nodes = [n.model_copy(update={"evaluation": {"mode": "machine"}}) if n.kind == "eval" else n for n in request.proposal.nodes]
        proposal = request.proposal.model_copy(update={"nodes": nodes})
    with pytest.raises(revision.ProposalRejectedError):
        await revision.preview_draft_revision(session, draft[0].flow_id, request.model_copy(update={"proposal": proposal}), operator)
    assert len((await records(session, draft[0].flow_id))[0]) == 1


async def test_superseded_address_cannot_be_reused(session, draft, operator):
    result, _ = await save(session, draft, operator)
    request = request_for(draft, proposal=draft[1], expected_plan_version=2, expected_plan_hash=result["plan_hash"])
    with pytest.raises(revision.DraftRevisionConflictError, match="Use a new address"):
        await revision.preview_draft_revision(session, draft[0].flow_id, request, operator)


async def test_execution_history_refuses_edit_even_if_node_was_reset(session, draft, operator):
    from src.orchestration.models import OrchestrationExecution

    node = (await reg.nodes_by_ref(session))["story-a"]
    session.add(
        OrchestrationExecution(
            org_id=reg.ORG_A,
            flow_id=draft[0].flow_id,
            node_id=node.id,
            phase="completed",
            status="succeeded",
            claim_id="historical-claim",
            claim_generation=1,
        )
    )
    await session.flush()
    with pytest.raises(revision.DraftRevisionConflictError, match="execution history"):
        await revision.preview_draft_revision(session, draft[0].flow_id, request_for(draft), operator)


async def test_saved_task_api_proposal_retains_six_issue_backed_waves(session, registrar, operator):
    from src.orchestration.proposal import LoopProposal

    document = json.loads((Path(__file__).parents[4] / "docs/task-api/flow-proposal.json").read_text())
    document["org_id"] = reg.ORG_A
    document["proposed_execution_policy"]["org_id"] = reg.ORG_A
    proposal = LoopProposal.model_validate(document)
    await reg.seed_org(session)
    initial = proposal.model_copy(update={"title": "Initial registered Task API draft"})
    registered, _ = await register_draft_proposal(session, initial, registrar)
    await session.commit()
    result, _ = await save(
        session,
        (registered, initial),
        operator,
        revision.DraftRevisionRequest(proposal=proposal, expected_plan_version=1, expected_plan_hash=registered.plan_hash),
    )
    stored = (await records(session, registered.flow_id))[0][-1].plan_document
    assert result["plan_version"] == 2
    assert len(stored["nodes"]) == 16
    assert sum(n["kind"] == "story" for n in stored["nodes"]) == 9
    evaluations = [n for n in stored["nodes"] if n["kind"] == "eval"]
    assert len(evaluations) == 6 and all(n["issue_ref"] and n["evaluation"] is None for n in evaluations)
    assert len({n["address"].split("/")[2] for n in stored["nodes"]}) == 6
    assert stored["proposed_execution_policy"] == document["proposed_execution_policy"]
    assert stored["execution_policy"] is None


async def test_stale_edit_cannot_overwrite_a_saved_revision(session, draft, operator):
    request = request_for(draft)
    preview = await revision.preview_draft_revision(session, draft[0].flow_id, request, operator)
    await save(session, draft, operator)
    stale = revision.SaveDraftRevisionRequest(
        **request.model_copy(update={"proposal": request.proposal.model_copy(update={"title": "Another edit"})}).model_dump(),
        expected_proposal_hash=preview["proposal_hash"],
    )
    with pytest.raises(revision.DraftRevisionConflictError):
        await revision.save_draft_revision(session, draft[0].flow_id, stale, operator)
    assert len((await records(session, draft[0].flow_id))[0]) == 2


@pytest.mark.parametrize("field,value", [("expected_plan_hash", "0" * 64), ("expected_plan_version", 3)])
async def test_wrong_base_is_refused(session, draft, operator, field, value):
    with pytest.raises(revision.DraftRevisionConflictError, match="current draft changed"):
        await revision.preview_draft_revision(session, draft[0].flow_id, request_for(draft, **{field: value}), operator)


async def test_changed_preview_hash_is_refused_without_writes(session, draft, operator):
    write = revision.SaveDraftRevisionRequest(**request_for(draft).model_dump(), expected_proposal_hash="0" * 64)
    with pytest.raises(revision.DraftRevisionConflictError) as error:
        await revision.save_draft_revision(session, draft[0].flow_id, write, operator)
    assert error.value.code == "stale_draft_preview"
    assert len((await records(session, draft[0].flow_id))[0]) == 1


@pytest.mark.parametrize("kind", sorted(APPROVAL_DECISION_KINDS))
async def test_any_prior_approval_refuses_draft_editing(session, draft, operator, kind):
    await OrchestrationRepository(session).append_decision(
        org_id=operator.org_id,
        flow_id=draft[0].flow_id,
        kind=kind,
        actor_id=operator.actor_id,
        actor_role=operator.actor_role,
        actor_kind=operator.actor_kind.value,
    )
    with pytest.raises(revision.DraftRevisionConflictError) as error:
        await revision.preview_draft_revision(session, draft[0].flow_id, request_for(draft), operator)
    assert error.value.code == "draft_already_approved"


@pytest.mark.parametrize("state,attempts", [("ready", 0), ("running", 1), ("pending", 1), ("passed", 1)])
async def test_started_nodes_refuse_editing(session, draft, operator, state, attempts):
    nodes = await reg.nodes_by_ref(session)
    nodes["story-a"].state = state
    nodes["story-a"].attempts = attempts
    await session.flush()
    with pytest.raises(revision.DraftRevisionConflictError) as error:
        await revision.preview_draft_revision(session, draft[0].flow_id, request_for(draft), operator)
    assert error.value.code == "draft_execution_started"


async def test_gate_cannot_be_replaced(session, draft, operator):
    request = request_for(draft)
    nodes = list(reversed(request.proposal.nodes))
    request = request.model_copy(update={"proposal": request.proposal.model_copy(update={"nodes": nodes})})
    with pytest.raises(revision.DraftRevisionConflictError, match="first wave"):
        await revision.preview_draft_revision(session, draft[0].flow_id, request, operator)


async def test_cross_tenant_flow_is_not_found(session, draft, operator):
    actor = ApprovalContext(org_id="other", actor_id=operator.actor_id, actor_role=operator.actor_role)
    with pytest.raises(revision.FlowNotFoundError):
        await revision.preview_draft_revision(session, draft[0].flow_id, request_for(draft), actor)


async def test_service_actor_cannot_revise(session, draft, registrar):
    with pytest.raises(revision.DraftRevisionConflictError) as error:
        await revision.preview_draft_revision(session, draft[0].flow_id, request_for(draft), registrar)
    assert error.value.code == "draft_revision_operator_required"


async def test_revised_gate_accepts_only_the_new_hash(session, draft, operator, access):
    from src.orchestration.adapters.github_comments import InputPath, apply_gate_answer_for_context
    from tests.orchestration.product_fixture import seed_product

    user_id = await reg.seed_principal(session, org_id=reg.ORG_A, role=AdminRole.ORG_ADMIN.value)
    await seed_product(session, org_id=reg.ORG_A, actor_id=reg.HUMAN_USER_ID, user_id=user_id)
    request = request_for(draft)
    request.proposal.product_id = f"product-{reg.ORG_A}"
    result, _ = await save(session, draft, operator, request)
    assert (await session.get(OrchestrationFlow, draft[0].flow_id)).product_id == request.proposal.product_id
    gate = (await reg.nodes_by_ref(session))["accept"]
    context = reg.token_context(reg.ORG_A, user_id=reg.HUMAN_USER_ID, account_type="human")
    stale = await apply_gate_answer_for_context(
        session,
        context=context,
        node_id=gate.id,
        approve=True,
        reason="Review revision binding",
        access=access,
        input_path=InputPath.DASHBOARD,
        expected_plan_hash=draft[0].plan_hash,
    )
    assert stale.status.value == "refused_stale_plan"
    assert gate.state == NodeState.AWAITING_GATE.value
    accepted = await apply_gate_answer_for_context(
        session,
        context=context,
        node_id=gate.id,
        approve=True,
        reason="Review revision binding",
        access=access,
        input_path=InputPath.DASHBOARD,
        expected_plan_hash=result["plan_hash"],
    )
    assert accepted.status.value == "applied"


async def test_failed_plan_write_rolls_back_graph_mutation(session, draft, operator, monkeypatch):
    request = request_for(draft)
    preview = await revision.preview_draft_revision(session, draft[0].flow_id, request, operator)

    async def fail(*args, **kwargs):
        raise RuntimeError("injected write failure")

    monkeypatch.setattr(OrchestrationRepository, "record_accepted_plan", fail)
    with pytest.raises(RuntimeError, match="injected"):
        await revision.save_draft_revision(
            session,
            draft[0].flow_id,
            revision.SaveDraftRevisionRequest(**request.model_dump(), expected_proposal_hash=preview["proposal_hash"]),
            operator,
        )
    nodes = await reg.nodes_by_ref(session)
    assert "story-new" not in nodes and nodes["story-b"].state == NodeState.PENDING.value
    plans, decisions = await records(session, draft[0].flow_id)
    assert len(plans) == len(decisions) == 1


@pytest.fixture
async def client(session, access, operator):
    from fastapi.responses import JSONResponse

    from src.shared.exceptions import BedrockGatewayError

    await reg.seed_principal(session, org_id=reg.ORG_A, role=AdminRole.ORG_ADMIN.value)
    app = FastAPI()
    app.include_router(router, prefix="/orchestration")

    @app.exception_handler(BedrockGatewayError)
    async def gateway_error(_request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def db():
        yield session

    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_current_user] = lambda: reg.token_context(reg.ORG_A, user_id=reg.HUMAN_USER_ID, account_type="human")
    app.dependency_overrides[get_access_control] = lambda: access
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http, app


async def test_http_preview_and_save(client, session, draft):
    http, _ = client
    base = f"/orchestration/flows/{draft[0].flow_id}/draft"
    body = request_for(draft).model_dump(mode="json")
    preview = await http.post(base + "/preview", json=body)
    assert preview.status_code == 200, preview.text
    result = await http.post(base + "/revise", json={**body, "expected_proposal_hash": preview.json()["proposal_hash"]})
    assert result.status_code == 200, result.text
    assert result.json()["plan_version"] == 2 and not result.json()["execution_authorized"]


async def test_http_permissions_are_checked_before_flow_lookup(client, access):
    from src.admin.exceptions import AccessDeniedError

    http, _ = client

    async def deny(*args, **kwargs):
        assert args[1] == Permission.PLAN_APPROVE
        raise AccessDeniedError("denied")

    # The application handles AccessDeniedError globally; here assert the
    # dependency refuses before the deliberately nonexistent flow is resolved.
    access.check_permission = deny
    response = await http.post(
        "/orchestration/flows/missing/draft/preview",
        json={
            "proposal": reg.gateless_proposal().model_dump(mode="json"),
            "expected_plan_version": 1,
            "expected_plan_hash": "0" * 64,
        },
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "case,status,code",
    [
        ("missing", 404, "flow_not_found"),
        ("tenant", 404, "flow_not_found"),
        ("stale", 409, "stale_draft_revision"),
        ("invalid", 422, "invalid_draft_revision"),
        ("service", 403, "access_denied"),
    ],
)
async def test_http_refusal_contracts(client, draft, session, case, status, code):
    http, app = client
    flow_id = draft[0].flow_id
    body = request_for(draft).model_dump(mode="json")
    if case == "missing":
        flow_id = "missing"
    elif case == "tenant":
        flow = await session.get(OrchestrationFlow, flow_id)
        flow.org_id = "other"
        await session.flush()
    elif case == "stale":
        body["expected_plan_hash"] = "0" * 64
    elif case == "invalid":
        body["proposal"]["org_id"] = "other"
    else:
        app.dependency_overrides[get_current_user] = lambda: reg.token_context(reg.ORG_A, user_id=reg.HUMAN_USER_ID, account_type="service")
    response = await http.post(f"/orchestration/flows/{flow_id}/draft/preview", json=body)
    assert response.status_code == status, response.text
    assert response.json().get("detail", response.json())["error"] == code


async def test_wave_display_edit_preserves_execution_and_replays(session, draft, operator):
    from src.orchestration.proposal import EpicMetadata, WaveMetadata

    before = await reg.nodes_by_ref(session)
    snapshot = {ref: (node.id, node.state, node.attempts) for ref, node in before.items()}
    authored = draft[1]
    _, epic, wave, _ = authored.nodes[0].address.split("/")
    metadata = [WaveMetadata(epic_ref=epic, wave_ref=wave, title="Contracts and design", description="Freeze and verify contracts.")]
    epic_metadata = [EpicMetadata(epic_ref=epic, title="Task APIs", description="Services need a dependable task lifecycle.")]
    proposal = authored.model_copy(update={"wave_metadata": metadata, "epic_metadata": epic_metadata})
    request = request_for(draft, proposal=proposal)
    result, write = await save(session, draft, operator, request)
    assert result["plan_version"] == 2
    assert result["plan_hash"] == draft[0].plan_hash
    assert result["execution_authorized"] is False
    after = await reg.nodes_by_ref(session)
    assert {ref: (node.id, node.state, node.attempts) for ref, node in after.items()} == snapshot
    plans, _ = await records(session, draft[0].flow_id)
    assert len(plans) == 2
    assert plans[-1].plan_document["wave_metadata"] == [item.model_dump(mode="json") for item in metadata]
    assert plans[-1].plan_document["epic_metadata"] == [item.model_dump(mode="json") for item in epic_metadata]
    assert plans[-1].plan_document["execution_policy"] is None
    assert plans[-1].plan_document["nodes"] == plans[0].plan_document["nodes"]
    assert plans[-1].plan_document["edges"] == plans[0].plan_document["edges"]
    assert plans[-1].plan_document["proposed_execution_policy"] == plans[0].plan_document["proposed_execution_policy"]
    assert (await session.get(OrchestrationFlow, draft[0].flow_id)).execution_paused is True
    replay = await revision.save_draft_revision(session, draft[0].flow_id, write, operator)
    assert replay["already_revised"] is True
    changed = proposal.model_copy(update={"wave_metadata": [metadata[0].model_copy(update={"title": "Changed wording"})]})
    assert revision.draft_hash(changed) != revision.draft_hash(proposal)
