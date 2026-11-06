"""Repeated owner checkpoints preserve history and dispatch only reviewed pins."""

import base64
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import tests.orchestration.test_maintenance_adoption as fixtures
from src.orchestration.accepted_node import accepted_node_contract
from src.orchestration.continuation import digest
from src.orchestration.maintenance_adoption import adopt_maintenance
from src.orchestration.maintenance_adoption_resolution import verified_adoption
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationDecision, OrchestrationExecution
from src.orchestration.policy_admission import load_in_force_policy
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.run_reports import OrchestrationRunReport, record_terminal

engine = fixtures.engine
session = fixtures.session
governed_work_ownership = fixtures.governed_work_ownership
healthy_policy_reservations = fixtures.healthy_policy_reservations
policy_budget_initializers = fixtures.policy_budget_initializers


async def adopt(session, b):
    preview = await fixtures.preview(session, b)
    return await adopt_maintenance(
        session,
        flow_id=b.node.flow_id,
        actor=b.actor,
        provider=b.provider,
        accept=True,
        request=b.request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
    )


def next_review(b, accepted, *, source="8", head="7"):
    before = b.request.source_revision
    pull = 9 + accepted["plan_version"]
    b.request = b.request.model_copy(
        update={
            "expected_plan_version": accepted["plan_version"],
            "expected_plan_hash": accepted["plan_hash"],
            "expected_attempt": b.node.attempts,
            "pull_number": pull,
            "source_revision": source * 40,
            "reviewed_head": head * 40,
        }
    )
    b.pr.update(number=pull, merge_commit_sha=source * 40, head={"sha": head * 40, "repo": {"id": 12345}})
    b.check["head_sha"] = head * 40

    async def request(_binding, method, path, **_kwargs):
        assert method == "GET"
        if path.endswith(f"/pulls/{b.request.pull_number}"):
            value = b.pr
        elif path.endswith("/commits/" + b.request.source_revision):
            value = {"sha": b.request.source_revision, "parents": [{"sha": before}, {"sha": b.request.reviewed_head}]}
        elif "/branches/" in path:
            value = {"commit": {"sha": b.request.source_revision}}
        elif "/contents/" in path:
            value = {
                "type": "file",
                "encoding": "base64",
                "size": len(fixtures.WORKFLOW),
                "content": base64.b64encode(fixtures.WORKFLOW).decode(),
            }
        else:
            value = {"id": 12345, "default_branch": "main"}
        return SimpleNamespace(json=lambda: copy.deepcopy(value))

    b.provider.request.side_effect = request
    b.provider.definition.return_value = SimpleNamespace(
        approved_revision=source * 40, source_revision=source * 40, dispatch_ref="main", dispatch_revision=source * 40
    )


async def resume(session, b, monkeypatch):
    from src.orchestration.controls import ResumeRequest, resume_node
    from src.orchestration.dispatch_pass import run_dispatch_pass

    monkeypatch.setattr("src.orchestration.operations_release_resolution.ReleaseProvider", lambda: b.provider)
    await resume_node(
        b.node.id,
        ResumeRequest(reason="Retry using the latest reviewed maintenance checkpoint."),
        SimpleNamespace(org_id=b.node.org_id, user_id=b.actor.actor_id),
        SimpleNamespace(check_permission=AsyncMock(), get_user_role=AsyncMock(return_value=[SimpleNamespace(value="org_admin")])),
        session,
        None,
    )
    dispatched = await run_dispatch_pass(session, fixtures.fixtures._config())
    assert dispatched.dispatched == 1, list(
        await session.scalars(select(OrchestrationDecision.reason).where(OrchestrationDecision.kind == "transition_rejected"))
    )
    return dispatched.pending[0].envelope


async def fail_attempt(session, b, envelope):
    from src.orchestration.results import observe_results

    report = await session.get(OrchestrationRunReport, envelope["message_id"])
    record_terminal(report, "failed", failure={"category": "deadline", "exit_code": 124})
    execution = await session.scalar(
        select(OrchestrationExecution).where(OrchestrationExecution.node_id == b.node.id, OrchestrationExecution.cycle == b.node.attempts)
    )
    for action in await session.scalars(
        select(OrchestrationAction).where(OrchestrationAction.execution_id == execution.id, OrchestrationAction.kind == "operations_task")
    ):
        action.status, action.detail = "succeeded", {**action.detail, "state": "done"}
    await session.commit()
    assert (await observe_results(session)).advanced == 1
    assert b.node.state == "failed" and execution.phase == execution.status == "concluded"
    # A real published head, still without an engine merge receipt, is covered
    # by the next owner's provider proof. Never invent a native merge receipt.
    session.add(
        OrchestrationAction(
            org_id=b.node.org_id,
            execution_id=execution.id,
            operation_key="published-second-attempt",
            kind="operations_repair_pr",
            status="succeeded",
            attempt=1,
            receipt_ref="github/acme/widgets/pull/10",
            detail={"head": "6" * 40},
        )
    )
    await session.flush()
    return report


@pytest.mark.parametrize("intervening_attempt", [False, True])
async def test_repeated_checkpoints_dispatch_latest_without_rewriting_history(session, monkeypatch, governed_work_ownership, intervening_attempt):
    b = await fixtures.arrange(session, monkeypatch)
    original_release = (await accepted_node_contract(session, b.node)).node.operational_release_binding
    original_terminal = copy.deepcopy(b.report.terminal_receipt)
    first = await adopt(session, b)
    first_receipt = await session.get(OrchestrationDecision, first["decision_id"])
    first_reason = first_receipt.reason
    reports = [b.report]
    if intervening_attempt:
        reports.append(await fail_attempt(session, b, await resume(session, b, monkeypatch)))
    attempts = b.node.attempts
    terminals = [copy.deepcopy(r.terminal_receipt) for r in reports]
    policy_before = (await load_in_force_policy(session, org_id=b.node.org_id, flow_id=b.node.flow_id)).policy
    plans = list(await session.scalars(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == b.node.flow_id)))
    documents = {p.id: copy.deepcopy(p.plan_document) for p in plans}
    next_review(b, first)
    second = await adopt(session, b)
    second_receipt = await session.get(OrchestrationDecision, second["decision_id"])
    second_reason = second_receipt.reason
    assert json.loads(second_reason)["prior_adoption_digest"] == digest(json.loads(first_reason))
    next_review(b, second, source="5", head="4")
    third = await adopt(session, b)
    current = await accepted_node_contract(session, b.node)
    release = current.node.operational_release_binding
    assert release.delivery_contract.required_checks == original_release.delivery_contract.required_checks
    assert release.delivery_contract.cleanup_required == original_release.delivery_contract.cleanup_required
    assert release.predecessors == original_release.predecessors
    assert release.build_prerequisites == original_release.build_prerequisites
    latest = await verified_adoption(session, b.node, current)
    assert latest["checkpoint_cycle"] == attempts
    assert latest["before_pins"]["source_revision"] == "8" * 40
    assert latest["prior_adoption_digest"] == digest(json.loads(second_reason))
    assert b.node.attempts == attempts and b.node.state == "failed"
    assert [r.terminal_receipt for r in reports] == terminals
    assert b.report.terminal_receipt == original_terminal
    assert first_receipt.reason == first_reason and second_receipt.reason == second_reason
    assert all(p.plan_document == documents[p.id] for p in plans)
    policy_after = (await load_in_force_policy(session, org_id=b.node.org_id, flow_id=b.node.flow_id)).policy
    excluded = {"policy_id", "policy_hash", "principal_id"}
    assert policy_before.model_dump(exclude=excluded) == policy_after.model_dump(exclude=excluded)
    assert not await session.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "operations_repair_merge"))
    contained = {call.args[1] for call in b.provider.contains.await_args_list}
    assert {fixtures.MERGE, "8" * 40, fixtures.PUBLISHED} <= contained
    if intervening_attempt:
        assert "6" * 40 in contained
    from src.orchestration.maintenance_adoption_resolution import prior_contract_for_adoption
    from src.orchestration.operations_release_resolution import operational_execution_pins

    prior_execution = await session.scalar(
        select(OrchestrationExecution).where(OrchestrationExecution.node_id == b.node.id, OrchestrationExecution.cycle == attempts)
    )
    prior_plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.flow_id == b.node.flow_id,
            OrchestrationAcceptedPlan.version == prior_execution.accepted_plan_version,
        )
    )
    args = dict(node=b.node, execution=prior_execution, run_id=reports[-1].run_id)
    assert await operational_execution_pins(session, contract=current, **args) is None
    historical = await prior_contract_for_adoption(session, b.node, current, prior_plan)
    assert historical is not None
    assert (
        await operational_execution_pins(session, contract=historical, **args)
        == reports[-1].dispatch_metadata["orchestration"]["deployment_revisions"]
    )
    dispatched = await resume(session, b, monkeypatch)
    metadata = dispatched["orchestration"]
    assert metadata["deployment_revisions"]["source_revision"] == "5" * 40
    assert metadata["release_binding"]["maintenance_adoption_id"] == third["decision_id"]
    assert b.node.attempts == attempts + 1
    from src.orchestration.operations_repair import repair_revisions

    assert (
        await repair_revisions(
            session,
            node_id=b.node.id,
            org_id=b.node.org_id,
            plan_version=third["plan_version"],
            cycle=b.node.attempts,
            pins=metadata["deployment_revisions"],
            contract=current,
            require_merged=True,
        )
        == metadata["deployment_revisions"]
    )


@pytest.mark.parametrize("fault", ["malformed_ancestor", "changed_ancestor", "changed_plan", "bound"])
async def test_every_ancestor_is_verified_before_another_checkpoint(session, monkeypatch, governed_work_ownership, fault):
    b = await fixtures.arrange(session, monkeypatch)
    first = await adopt(session, b)
    next_review(b, first)
    second = await adopt(session, b)
    next_review(b, second, source="5", head="4")
    if fault == "bound":
        monkeypatch.setattr("src.orchestration.maintenance_adoption_resolution.MAX_CHECKPOINTS", 1)
    elif fault == "changed_plan":
        # Simulate corrupt persisted history; receipt validation must notice
        # even though the latest plan still has the expected request hash.
        b.f.plan.plan_document = {**b.f.plan.plan_document, "spec_revision": "changed-history"}
    else:
        receipt = await session.get(OrchestrationDecision, first["decision_id"])
        data = json.loads(receipt.reason)
        if fault == "malformed_ancestor":
            del data["proof"]["source_revision"]
        else:
            data["proof"]["owner_review_ref"] = "different-owner-review"
        original_get = session.get

        async def corrupted_read(model, identity, **kwargs):
            if model is OrchestrationDecision and identity == receipt.id:
                return SimpleNamespace(
                    **{key: getattr(receipt, key) for key in ("id", "org_id", "flow_id", "node_id", "kind", "actor_kind", "actor_role", "actor_id")},
                    reason=json.dumps(data),
                )
            return await original_get(model, identity, **kwargs)

        monkeypatch.setattr(session, "get", corrupted_read)
    with pytest.raises(CycleBlockedError, match="maintenance_receipt_"):
        await fixtures.preview(session, b)
    assert b.node.attempts == 1 and b.node.state == "failed"


@pytest.mark.parametrize("fault", ["execution", "action", "changed_preview", "revoked"])
async def test_repeat_refuses_unsettled_work_or_changed_authority(session, monkeypatch, governed_work_ownership, fault):
    b = await fixtures.arrange(session, monkeypatch)
    first = await adopt(session, b)
    old_reason = (await session.get(OrchestrationDecision, first["decision_id"])).reason
    next_review(b, first)
    preview = await fixtures.preview(session, b)
    if fault == "execution":
        b.execution.status = "running"
    elif fault == "action":
        session.add(
            OrchestrationAction(
                org_id=b.node.org_id,
                execution_id=b.execution.id,
                operation_key="pending-effect",
                kind="operations_continuation_deploy",
                status="unknown",
            )
        )
    elif fault == "changed_preview":
        b.request = b.request.model_copy(update={"reason": "A different reviewed reason must require its own new preview."})
    else:
        monkeypatch.setattr("src.shared.identity.workflow_access.is_workflow_member", AsyncMock(return_value=False))
    await session.flush()
    with pytest.raises(CycleBlockedError, match="execution_outstanding|effects_unresolved|preview_changed|principal_authority_revoked"):
        await adopt_maintenance(
            session,
            flow_id=b.node.flow_id,
            actor=b.actor,
            provider=b.provider,
            accept=True,
            request=b.request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
        )
    assert (await session.get(OrchestrationDecision, first["decision_id"])).reason == old_reason
    assert len(list(await session.scalars(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.flow_id == b.node.flow_id)))) == 2


async def test_later_direct_ancestry_keeps_original_squash_proof(session, monkeypatch, governed_work_ownership):
    from tests.orchestration.test_maintenance_squash_history import arrange

    b = await arrange(session, monkeypatch)
    first = await adopt(session, b)
    original = await session.get(OrchestrationDecision, first["decision_id"])
    original_reason = original.reason
    next_review(b, first)
    # The new source directly includes these heads, but the earlier adoption
    # still depends on its exact, immutable tree-equality squash proof.
    b.request = b.request.model_copy(update={"squashed_repair_history": []})
    await adopt(session, b)
    latest = await verified_adoption(session, b.node, await accepted_node_contract(session, b.node))
    assert "squashed_repair_history" not in latest["proof"]
    assert original.reason == original_reason
    assert json.loads(original_reason)["proof"]["squashed_repair_history"]
    assert latest["prior_adoption_digest"] == digest(json.loads(original_reason))


async def test_full_valid_chain_refuses_append_before_creating_an_unreadable_checkpoint(session, monkeypatch, governed_work_ownership):
    b = await fixtures.arrange(session, monkeypatch)
    first = await adopt(session, b)
    next_review(b, first)
    second = await adopt(session, b)
    next_review(b, second, source="5", head="4")
    preview = await fixtures.preview(session, b)
    monkeypatch.setattr("src.orchestration.maintenance_adoption_resolution.MAX_CHECKPOINTS", 2)
    current = await accepted_node_contract(session, b.node)
    assert (await verified_adoption(session, b.node, current))["pins"]["source_revision"] == "8" * 40
    with pytest.raises(CycleBlockedError, match="maintenance_receipt_chain_limit"):
        await fixtures.preview(session, b)
    with pytest.raises(CycleBlockedError, match="maintenance_receipt_chain_limit"):
        await adopt_maintenance(
            session,
            flow_id=b.node.flow_id,
            actor=b.actor,
            provider=b.provider,
            accept=True,
            request=b.request.model_copy(update={"expected_snapshot": preview["snapshot"]}),
        )
    assert (await accepted_node_contract(session, b.node)).plan_version == second["plan_version"]
    assert b.node.attempts == 1 and b.node.state == "failed"


@pytest.mark.parametrize("tamper", [None, "target", "repair_paths"])
@pytest.mark.parametrize("intervening_attempt", [False, True])
async def test_original_native_repairs_use_historical_contract_and_authority(
    session, monkeypatch, governed_work_ownership, tamper, intervening_attempt
):
    b = await fixtures.arrange(session, monkeypatch, repair_authority=True)
    if intervening_attempt:
        checkpoint = await adopt(session, b)
        report = await fail_attempt(session, b, await resume(session, b, monkeypatch))
        execution = await session.scalar(
            select(OrchestrationExecution).where(OrchestrationExecution.node_id == b.node.id, OrchestrationExecution.cycle == b.node.attempts)
        )
        next_review(b, checkpoint, source="a", head="b")
    else:
        report, execution = b.report, b.execution
    policy = (await load_in_force_policy(session, org_id=b.node.org_id, flow_id=b.node.flow_id)).policy
    pins = report.dispatch_metadata["orchestration"]["deployment_revisions"]
    merge = OrchestrationAction(
        org_id=b.node.org_id,
        execution_id=execution.id,
        kind="operations_repair_merge",
        status="succeeded",
        attempt=1,
        operation_key="operations-repair:77:" + "3" * 40,
        receipt_ref=f"github/{fixtures.fixtures.REPO}/pull/77",
        detail={
            "pull_number": 77,
            "head": "3" * 40,
            "merge_sha": "2" * 40,
            "repository": fixtures.fixtures.REPO,
            "default_branch": "main",
            "policy_hash": policy.policy_hash,
            "workflows": ["gateway-deploy.yml"],
            "repair_paths": sorted(policy.repair_paths),
            "before_source_revision": pins["source_revision"],
            "before_definition_revision": pins["definition_revision"],
            "claim_generation": execution.claim_generation,
            "run_id": report.run_id,
        },
    )
    session.add(merge)
    await session.flush()
    first = await adopt(session, b)
    first_data = json.loads((await session.get(OrchestrationDecision, first["decision_id"])).reason)
    assert first_data["before_pins"]["source_revision"] == "2" * 40
    old_detail = copy.deepcopy(merge.detail)
    adopted_source = b.request.source_revision
    next_review(b, first)
    if tamper:
        # Change the stored effect beneath its immutable adoption checkpoint.
        key, value = ("repository", "other/repository") if tamper == "target" else ("repair_paths", ["unapproved/path"])
        merge.detail = {**merge.detail, key: value}
        await session.flush()
        with pytest.raises(CycleBlockedError, match="receipt_history_changed"):
            await fixtures.preview(session, b)
    else:
        second = await adopt(session, b)
        assert merge.detail == old_detail
        second_data = json.loads((await session.get(OrchestrationDecision, second["decision_id"])).reason)
        assert second_data["before_pins"]["source_revision"] == adopted_source
        assert second_data["repair_history"] == first_data["repair_history"]
        assert b.node.attempts == (2 if intervening_attempt else 1)
