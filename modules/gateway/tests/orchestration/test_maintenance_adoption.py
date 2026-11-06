"""Maintenance adoption preserves real execution history and fails closed on drift."""

import base64
import copy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import tests.orchestration.test_operations_release_resolution as fixtures
from src.orchestration.accepted_node import accepted_node_contract
from src.orchestration.compile import HASH_EXCLUDED_FIELDS, HASH_OMITTED_WHEN_ABSENT, ApprovalContext, plan_hash
from src.orchestration.maintenance_adoption import MaintenanceAdoptionRequest, adopt_maintenance
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationNode
from src.orchestration.proposal import LoopProposal, validate_proposal
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import OrchestrationRunReport, record_terminal

engine = fixtures.engine
session = fixtures.session
governed_work_ownership = fixtures.governed_work_ownership
healthy_policy_reservations = fixtures.healthy_policy_reservations
policy_budget_initializers = fixtures.policy_budget_initializers

HEAD, MERGE, PUBLISHED = "e" * 40, "f" * 40, "9" * 40
ROLE = "arn:aws:iam::123456789012:role/deployment"
WORKFLOW = b"name: reviewed maintenance\n"


async def arrange(session, monkeypatch, *, repair_authority=False):
    async def configure(flow, node, plan, manifest, provider):
        from src.orchestration.delivery_contract import DeliveryContract

        document = copy.deepcopy(plan.plan_document)
        document.update(org_id=flow.org_id, title=flow.title, spec_revision="maintenance-test-v1", intent_ref="4120")
        target = document["nodes"][-1]
        target["issue_ref"] = node.issue_ref
        node.repository_identity = target["repository_identity"]
        entry = manifest.entries[0]
        target["operational_release_binding"]["delivery_contract"] = DeliveryContract(
            schema_version=2,
            connection_id=entry.connection_id,
            resource_kind=entry.resource_kind,
            resource_id=entry.resource_id,
            baseline_revision=entry.artifact_revision,
            definition_revision=entry.workflow.definition_revision,
            required_checks=["runtime"],
            cleanup_required=True,
            job_mutation_role_arn=ROLE,
        ).model_dump(mode="json")
        for index, doc in enumerate(document["nodes"][:-1]):
            n = await session.scalar(
                select(OrchestrationNode).where(OrchestrationNode.flow_id == flow.id, OrchestrationNode.node_ref == f"build-{index + 1}")
            )
            n.issue_ref = doc["issue_ref"] = str(4197 + index)
            document["edges"].append({"from_address": doc["address"], "to_address": target["address"]})
        evaluation = await fixtures._make_node(session, flow, node_ref="evaluation", state="pending", kind="eval", issue_ref="4199")
        eval_address = f"{flow.slug}/{evaluation.epic_ref}/{evaluation.wave_ref}/{evaluation.node_ref}"
        document["nodes"].append({"address": eval_address, "kind": "eval", "title": "Final validation", "issue_ref": "4199"})
        document["edges"].append({"from_address": target["address"], "to_address": eval_address})
        proposal = LoopProposal.model_validate(document)
        assert not validate_proposal(proposal)
        plan.plan_document, plan.plan_hash = proposal.model_dump(mode="json"), plan_hash(proposal)
        from src.orchestration.models import OrchestrationEdge

        all_nodes = list(await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == flow.id)))
        ids = {f"{flow.slug}/{n.epic_ref}/{n.wave_ref}/{n.node_ref}": n.id for n in all_nodes}
        session.add_all(
            [
                OrchestrationEdge(org_id=flow.org_id, flow_id=flow.id, from_node_id=ids[e.from_address], to_node_id=ids[e.to_address])
                for e in proposal.edges
            ]
        )
        await session.flush()

    fixture = await fixtures._dispatched_release(session, monkeypatch, retry=True, repair_authority=repair_authority, prepare_release=configure)
    node = fixture.node
    report = await session.get(OrchestrationRunReport, fixture.first_pass.pending[0].envelope["message_id"])
    text = "Workflow drift blocked delivery before qualification."
    report.operations_report_receipt = {
        "recorded": True,
        "run_id": report.run_id,
        "attempt": 1,
        "node_id": node.id,
        "receipt_id": f"operations-report:{report.run_id}",
        "report": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    record_terminal(report, "failed", failure={"category": "deadline", "exit_code": 124})
    for action in (await session.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_task"))).all():
        action.status, action.detail = "succeeded", {**action.detail, "state": "done"}
    await session.commit()
    from src.orchestration.results import observe_results

    assert (await observe_results(session)).advanced == 1
    execution = await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == node.id))
    publication = OrchestrationAction(
        org_id=node.org_id,
        execution_id=execution.id,
        operation_key="published-old-head",
        kind="operations_repair_pr",
        status="succeeded",
        attempt=1,
        receipt_ref="github/acme/widgets/pull/7",
        detail={"head": PUBLISHED},
    )
    session.add(publication)
    await session.flush()
    request = MaintenanceAdoptionRequest(
        node_id=node.id,
        expected_plan_version=fixture.plan.version,
        expected_plan_hash=fixture.plan.plan_hash,
        expected_attempt=1,
        pull_number=9,
        reviewed_head=HEAD,
        source_revision=MERGE,
        workflow_sha256=hashlib.sha256(WORKFLOW).hexdigest(),
        reviewed_paths=["modules/worker.py"],
        required_checks=["qualified"],
        job_mutation_role_arn=ROLE,
        review_ref="owner-reviewed:maintenance-head",
        reason="Adopt reviewed maintenance without losing any story history.",
    )
    pr = {
        "number": 9,
        "merged": True,
        "merge_commit_sha": MERGE,
        "head": {"sha": HEAD, "repo": {"id": 12345}},
        "base": {"ref": "main", "repo": {"id": 12345}},
    }
    check = {"id": 1, "name": "qualified", "head_sha": HEAD, "status": "completed", "conclusion": "success"}

    async def request_provider(binding, method, path, **kwargs):
        assert method == "GET"
        if path.endswith("/pulls/9"):
            value = pr
        elif path.endswith("/commits/" + MERGE):
            value = {"sha": MERGE, "parents": [{"sha": fixtures.SOURCE}, {"sha": HEAD}]}
        elif "/branches/" in path:
            value = {"commit": {"sha": MERGE}}
        elif "/contents/" in path:
            value = {"type": "file", "encoding": "base64", "size": len(WORKFLOW), "content": base64.b64encode(WORKFLOW).decode()}
        else:
            value = {"id": 12345, "default_branch": "main"}
        return SimpleNamespace(json=lambda: copy.deepcopy(value))

    async def pages(_binding, path, *args):
        return [{"filename": "modules/worker.py"}] if path.endswith("/files") else [copy.deepcopy(check)]

    provider = SimpleNamespace(
        request=AsyncMock(side_effect=request_provider),
        pages=AsyncMock(side_effect=pages),
        contains=AsyncMock(),
        definition=AsyncMock(
            return_value=SimpleNamespace(approved_revision=MERGE, source_revision=MERGE, dispatch_ref="main", dispatch_revision=MERGE)
        ),
    )
    actor = ApprovalContext(org_id=node.org_id, actor_id=fixtures.policy_fixtures.APPROVER, actor_role="platform_admin")
    return SimpleNamespace(
        f=fixture,
        node=node,
        report=report,
        execution=execution,
        publication=publication,
        request=request,
        provider=provider,
        actor=actor,
        pr=pr,
        check=check,
    )


async def preview(session, b):
    return await adopt_maintenance(session, flow_id=b.node.flow_id, actor=b.actor, request=b.request, provider=b.provider)


@pytest.mark.parametrize("explicit_null", [False, True])
def test_legacy_release_binding_keeps_literal_plan_hash(explicit_null):
    from src.orchestration.shared_budget import accepted_document_hash as budget_hash
    from src.orchestration.shared_window import accepted_document_hash as window_hash
    from tests.orchestration.test_proposal import address, make_proposal

    document = make_proposal().model_dump(mode="json")
    binding = {
        "predecessors": [{"address": address("build"), "receipt_kind": "verified_merge"}],
        "build_prerequisites": [address("build")],
        "repository": "demo/repo",
        "target": "stage",
        "component": "security",
        "workflows": ["security-deploy.yml"],
        "definition_predecessor": None,
        "default_branch": "main",
    }
    if explicit_null:
        binding["maintenance_adoption_id"] = None
    document["nodes"][0]["operational_release_binding"] = binding
    proposal = LoopProposal.model_validate(document)
    # Preserve pre-field plans and plans accepted while the shadowed serializer
    # emitted null. Pydantic uses only the last model serializer on a class.
    legacy_hash = (
        "d9c69c06eecd88c6b0a675f9407e9325569dd6bda554de8e3de93fc8fc0958d4"
        if explicit_null
        else "baf824832c3b51c9337faa939c998a18853bd219ae449efb221ad38ceb0496c9"
    )
    assert plan_hash(proposal) == legacy_hash
    assert budget_hash(document) == window_hash(SimpleNamespace(plan_document=document)) == legacy_hash
    for mode in ("python", "json"):
        assert proposal.model_dump(mode=mode)["nodes"][0]["operational_release_binding"] == binding
    assert plan_hash(LoopProposal.model_validate(proposal.model_dump(mode="json"))) == legacy_hash
    binding["maintenance_adoption_id"] = "12345678-1234-1234-1234-123456789012"
    adopted = LoopProposal.model_validate(document)
    assert adopted.model_dump(mode="json")["nodes"][0]["operational_release_binding"] == binding
    assert plan_hash(adopted) != legacy_hash
    binding["maintenance_adoption_id"] = "12345678-1234-1234-1234-123456789013"
    assert plan_hash(LoopProposal.model_validate(document)) != plan_hash(adopted)


async def test_legacy_plan_preview_preserves_original_document_and_refuses_real_change(session, monkeypatch, governed_work_ownership):
    b = await arrange(session, monkeypatch)
    document = copy.deepcopy(b.f.plan.plan_document)
    for node in document["nodes"]:
        if node.get("operational_release_binding") is not None:
            node["operational_release_binding"].pop("maintenance_adoption_id", None)
    # Reconstruct the stored pre-maintenance representation, without passing it
    # through the new schema that caused the regression.
    executable = {key: value for key, value in copy.deepcopy(document).items() if key not in HASH_EXCLUDED_FIELDS}
    for key in HASH_OMITTED_WHEN_ABSENT:
        if executable.get(key) is None:
            executable.pop(key, None)
    for node in executable["nodes"]:
        if node.get("evaluation") is None:
            node.pop("evaluation", None)
    digest = hashlib.sha256(json.dumps(executable, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    b.f.plan.plan_document, b.f.plan.plan_hash = document, digest
    b.request = b.request.model_copy(update={"expected_plan_hash": digest})
    await session.flush()
    result = await preview(session, b)
    assert not result["accepted"]
    assert b.f.plan.plan_document == document and b.f.plan.plan_hash == digest
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1
    changed = copy.deepcopy(document)
    changed_binding = next(node["operational_release_binding"] for node in changed["nodes"] if node.get("operational_release_binding"))
    changed_binding["component"] = "changed-component"
    b.f.plan.plan_document = changed
    await session.flush()
    with pytest.raises(CycleBlockedError, match="maintenance_accepted_document_changed"):
        await preview(session, b)


async def test_adoption_creates_versioned_receipt_and_preserves_failures(session, monkeypatch, governed_work_ownership):
    b = await arrange(session, monkeypatch)
    before = copy.deepcopy(b.report.terminal_receipt)
    original = copy.deepcopy(b.f.plan.plan_document)
    value = await preview(session, b)
    result = await adopt_maintenance(
        session,
        flow_id=b.node.flow_id,
        actor=b.actor,
        request=b.request.model_copy(update={"expected_snapshot": value["snapshot"]}),
        provider=b.provider,
        accept=True,
    )
    assert result["accepted"] and result["plan_version"] == 2
    replay = await adopt_maintenance(
        session,
        flow_id=b.node.flow_id,
        actor=b.actor,
        provider=b.provider,
        accept=True,
        request=b.request.model_copy(update={"expected_snapshot": value["snapshot"]}),
    )
    assert replay["decision_id"] == result["decision_id"] and replay["created"] is False
    assert b.node.state == "failed" and b.node.attempts == 1
    assert b.report.terminal_receipt == before and b.f.plan.plan_document == original
    assert not await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_repair_merge"))
    from src.orchestration.maintenance_adoption_resolution import verified_adoption

    current = await accepted_node_contract(session, b.node)
    receipt = await verified_adoption(session, b.node, current)
    assert receipt["proof"]["included_published_heads"] == [PUBLISHED]
    assert receipt["pins"]["source_revision"] == MERGE
    assert current.node.operational_release_binding.delivery_contract.required_checks == ["runtime"]
    # Exercise the normal resume and dispatch boundary: the *new workspace*
    # receives adopted pins, not just the later workflow API call.
    from src.orchestration.controls import ResumeRequest, resume_node
    from src.orchestration.dispatch_pass import run_dispatch_pass

    monkeypatch.setattr("src.orchestration.operations_release_resolution.ReleaseProvider", lambda: b.provider)
    await resume_node(
        b.node.id,
        ResumeRequest(reason="Retry using the reviewed maintenance adoption."),
        SimpleNamespace(org_id=b.node.org_id, user_id=b.actor.actor_id),
        SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=[SimpleNamespace(value="org_admin")])),
        session,
        None,
    )
    dispatched = await run_dispatch_pass(session, fixtures._config())
    assert dispatched.dispatched == 1, list(
        await session.scalars(select(OrchestrationDecision.reason).where(OrchestrationDecision.kind == "transition_rejected"))
    )
    metadata = dispatched.pending[0].envelope["orchestration"]
    assert metadata["deployment_revisions"]["source_revision"] == MERGE
    assert metadata["release_binding"]["maintenance_adoption_id"] == result["decision_id"]
    assert b.node.attempts == 2 and b.report.terminal_receipt == before
    from src.orchestration.operations_repair import RepairMergeRequiredError, repair_revisions

    kwargs = dict(
        node_id=b.node.id, org_id=b.node.org_id, plan_version=2, cycle=2, pins=metadata["deployment_revisions"], contract=current, require_merged=True
    )
    assert await repair_revisions(session, **kwargs) == metadata["deployment_revisions"]
    second = await session.scalar(
        select(OrchestrationExecution).where(OrchestrationExecution.node_id == b.node.id, OrchestrationExecution.cycle == 2)
    )
    session.add(
        OrchestrationAction(
            org_id=b.node.org_id,
            execution_id=second.id,
            operation_key="new-publication",
            kind="operations_repair_pr",
            status="succeeded",
            attempt=1,
            receipt_ref="github/acme/widgets/pull/10",
            detail={"head": "7" * 40},
        )
    )
    await session.flush()
    with pytest.raises(RepairMergeRequiredError):
        await repair_revisions(session, **kwargs)


@pytest.mark.parametrize("change", ["publication", "role", "check"])
async def test_changed_preview_is_rejected_before_amendment(session, monkeypatch, governed_work_ownership, change):
    b = await arrange(session, monkeypatch)
    value = await preview(session, b)
    if change == "publication":
        b.publication.detail = {"head": "8" * 40}
    elif change == "role":
        b.request = b.request.model_copy(update={"job_mutation_role_arn": ROLE + "-other"})
    else:
        b.check["id"] = 2
    await session.flush()
    with pytest.raises(CycleBlockedError, match="preview_changed"):
        await adopt_maintenance(
            session,
            flow_id=b.node.flow_id,
            actor=b.actor,
            provider=b.provider,
            accept=True,
            request=b.request.model_copy(update={"expected_snapshot": value["snapshot"]}),
        )
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1


@pytest.mark.parametrize("fault", ["expired", "revoked"])
async def test_expired_or_revoked_authority_cannot_adopt(session, monkeypatch, governed_work_ownership, fault):
    from datetime import UTC, datetime, timedelta

    from src.orchestration.execution_policy import ExecutionPolicy, policy_hash

    b = await arrange(session, monkeypatch)
    if fault == "expired":
        document = copy.deepcopy(b.f.plan.plan_document)
        policy = ExecutionPolicy.model_validate(document["execution_policy"])
        policy.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        policy.policy_hash = policy_hash(policy)
        document["execution_policy"] = policy.model_dump(mode="json")
        b.f.plan.plan_document = document
        b.f.plan.plan_hash = plan_hash(LoopProposal.model_validate(document))
        b.request = b.request.model_copy(update={"expected_plan_hash": b.f.plan.plan_hash})
    else:
        monkeypatch.setattr("src.shared.identity.workflow_access.is_workflow_member", AsyncMock(return_value=False))
    await session.flush()
    with pytest.raises(CycleBlockedError, match="policy_expired" if fault == "expired" else "principal_authority_revoked"):
        await preview(session, b)


async def test_adoption_requires_every_published_head_to_be_included(session, monkeypatch, governed_work_ownership):
    b = await arrange(session, monkeypatch)

    async def contains(binding, source, actual):
        if source == PUBLISHED:
            raise CycleBlockedError("deployment_newer_release_does_not_contain_merge")

    b.provider.contains.side_effect = contains
    with pytest.raises(CycleBlockedError, match="does_not_contain"):
        await preview(session, b)


async def test_deleted_old_publication_invalidates_checkpoint(session, monkeypatch, governed_work_ownership):
    b = await arrange(session, monkeypatch)
    value = await preview(session, b)
    await adopt_maintenance(
        session,
        flow_id=b.node.flow_id,
        actor=b.actor,
        provider=b.provider,
        accept=True,
        request=b.request.model_copy(update={"expected_snapshot": value["snapshot"]}),
    )
    await session.delete(b.publication)
    await session.flush()
    from src.orchestration.maintenance_adoption_resolution import verified_adoption

    with pytest.raises(CycleBlockedError, match="receipt_history_changed"):
        await verified_adoption(session, b.node, await accepted_node_contract(session, b.node))


async def test_never_dispatched_release_keeps_zero_attempts_and_pause(session, monkeypatch, governed_work_ownership):
    from src.orchestration.models import OrchestrationEdge

    b = await arrange(session, monkeypatch)
    fresh = await fixtures._make_node(session, b.f.flow, node_ref="not-started", state="ready", issue_ref="4201")
    fresh.repository_identity, fresh.provider_repository_id = fixtures.REPO, 12345
    document = copy.deepcopy(b.f.plan.plan_document)
    source = next(n for n in document["nodes"] if n.get("executor"))
    added = copy.deepcopy(source)
    added.update(address=f"{b.f.flow.slug}/{fresh.epic_ref}/{fresh.wave_ref}/{fresh.node_ref}", issue_ref="4201", provider_repository_id=12345)
    evaluation = next(n for n in document["nodes"] if n["kind"] == "eval")
    document["nodes"].append(added)
    document["edges"].extend(
        [
            *[{"from_address": p["address"], "to_address": added["address"]} for p in added["operational_release_binding"]["predecessors"]],
            {"from_address": added["address"], "to_address": evaluation["address"]},
        ]
    )
    proposal = LoopProposal.model_validate(document)
    assert not validate_proposal(proposal)
    b.f.plan.plan_document, b.f.plan.plan_hash = proposal.model_dump(mode="json"), plan_hash(proposal)
    all_nodes = list(await session.scalars(select(OrchestrationNode).where(OrchestrationNode.flow_id == b.f.flow.id)))
    ids = {f"{b.f.flow.slug}/{n.epic_ref}/{n.wave_ref}/{n.node_ref}": n.id for n in all_nodes}
    session.add_all(
        [
            OrchestrationEdge(org_id=fresh.org_id, flow_id=fresh.flow_id, from_node_id=ids[e.from_address], to_node_id=ids[e.to_address])
            for e in proposal.edges
            if fresh.id in (ids[e.from_address], ids[e.to_address])
        ]
    )
    b.f.flow.execution_paused = True
    b.node, b.request = fresh, b.request.model_copy(update={"node_id": fresh.id, "expected_attempt": 0, "expected_plan_hash": b.f.plan.plan_hash})
    await session.flush()
    value = await preview(session, b)
    result = await adopt_maintenance(
        session,
        flow_id=fresh.flow_id,
        actor=b.actor,
        provider=b.provider,
        accept=True,
        request=b.request.model_copy(update={"expected_snapshot": value["snapshot"]}),
    )
    assert result["accepted"] and fresh.attempts == 0 and fresh.state == "ready" and b.f.flow.execution_paused
    assert not await session.scalar(select(OrchestrationExecution).where(OrchestrationExecution.node_id == fresh.id))


async def test_existing_retry_supplement_survives_without_increasing_again(session, monkeypatch, governed_work_ownership):
    from src.orchestration.policy_admission import load_in_force_policy
    from src.orchestration.shared_retry import RetryIncreaseRequest, accept_retry_increase, preview_retry_increase

    b = await arrange(session, monkeypatch)
    increase = RetryIncreaseRequest(
        expected_plan_version=1,
        expected_plan_hash=b.f.plan.plan_hash,
        max_attempts_per_node=4,
        reason="An existing bounded owner-authorized retry increase.",
    )
    prepared = await preview_retry_increase(session, flow_id=b.node.flow_id, actor=b.actor, request=increase)
    supplement = await accept_retry_increase(
        session, flow_id=b.node.flow_id, actor=b.actor, request=increase.model_copy(update={"expected_snapshot": prepared["snapshot"]})
    )
    value = await preview(session, b)
    await adopt_maintenance(
        session,
        flow_id=b.node.flow_id,
        actor=b.actor,
        provider=b.provider,
        accept=True,
        request=b.request.model_copy(update={"expected_snapshot": value["snapshot"]}),
    )
    policy = (await load_in_force_policy(session, org_id=b.node.org_id, flow_id=b.node.flow_id)).policy
    assert policy.limits.max_attempts_per_node == 4 and policy._shared_retry_decision_id == supplement["decision_id"]
    assert b.node.attempts == 1 and b.node.state == "failed"


async def test_malformed_adoption_receipt_is_a_bounded_refusal(session, monkeypatch, governed_work_ownership):
    import json

    from src.orchestration.maintenance_adoption_resolution import verified_adoption

    b = await arrange(session, monkeypatch)
    value = await preview(session, b)
    result = await adopt_maintenance(
        session,
        flow_id=b.node.flow_id,
        actor=b.actor,
        provider=b.provider,
        accept=True,
        request=b.request.model_copy(update={"expected_snapshot": value["snapshot"]}),
    )
    receipt = await session.get(OrchestrationDecision, result["decision_id"])
    detail = json.loads(receipt.reason)
    del detail["proof"]["source_revision"]
    original_get = session.get

    async def corrupted_read(model, identity, **kwargs):
        if model is OrchestrationDecision and identity == receipt.id:
            return SimpleNamespace(
                id=receipt.id,
                org_id=receipt.org_id,
                flow_id=receipt.flow_id,
                node_id=receipt.node_id,
                kind=receipt.kind,
                actor_kind=receipt.actor_kind,
                actor_role=receipt.actor_role,
                actor_id=receipt.actor_id,
                reason=json.dumps(detail),
            )
        return await original_get(model, identity, **kwargs)

    monkeypatch.setattr(session, "get", corrupted_read)
    with pytest.raises(CycleBlockedError, match="maintenance_receipt_unverified"):
        await verified_adoption(session, b.node, await accepted_node_contract(session, b.node))


@pytest.mark.parametrize(
    "fault,code",
    [
        ("execution", "execution_outstanding"),
        ("check", "checks_unverified"),
        ("merge", "merge_changed"),
        ("source", "merge_changed"),
        ("role", "role_account_changed"),
        ("publication", "publication_head_missing"),
    ],
)
async def test_refusals_do_not_create_new_plan(session, monkeypatch, governed_work_ownership, fault, code):
    b = await arrange(session, monkeypatch)
    if fault == "execution":
        b.execution.status = "running"
    elif fault == "check":
        b.check["conclusion"] = "failure"
    elif fault == "merge":
        b.pr["merged"] = False
    elif fault == "source":
        b.request = b.request.model_copy(update={"source_revision": "8" * 40})
    elif fault == "role":
        b.request = b.request.model_copy(update={"job_mutation_role_arn": ROLE.replace("123456789012", "987654321098")})
    else:
        b.publication.detail = {}
    await session.flush()
    with pytest.raises(CycleBlockedError, match=code):
        await preview(session, b)
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan)))) == 1
    assert not await session.scalar(select(OrchestrationDecision).where(OrchestrationDecision.kind == "maintenance_release_adopted"))
