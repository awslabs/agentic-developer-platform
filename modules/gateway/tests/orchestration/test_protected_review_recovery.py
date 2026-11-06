"""Real claim/grant recovery preserves failures and normal retry allowance."""

# ruff: noqa: F811

import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select, update

from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision
from src.orchestration.protected_review_recovery import KIND, request_recovery, verified_decision
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_recovery import ReviewRecoveryRequest
from tests.orchestration.test_review_cycle import ORG, cycle, legacy_delivery, pg_server, pg_url, review, state, store, tick  # noqa: F401

ASTRA = "openai.gpt-6-astra"
SOL = "openai.gpt-6-sol"


async def configured_reviewer(ctx, monkeypatch):
    """Real SQL principal/preferences and catalogue, then real sealed bootstrap."""
    from src.agentauth.launch_configuration import resolve_launch_configuration
    from src.agentauth.model_policy import _contract_maps, _persist_snapshot
    from src.shared.models.base import Base
    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import Organization, Team, TeamMembership, User
    from src.shared.models.persona_models import PersonaModelPolicySetting, PersonaModelPreference, PersonaPlatformDefault
    from src.shared.models.vault import UserIdentity
    from tests.agentauth.test_model_policy import live_snapshot

    async with ctx.factory() as db:
        connection = await db.connection()
        models = [
            Organization,
            User,
            UserIdentity,
            Team,
            TeamMembership,
            TenantMembership,
            PersonaModelPreference,
            PersonaModelPolicySetting,
            PersonaPlatformDefault,
        ]
        await connection.run_sync(lambda conn: Base.metadata.create_all(conn, tables=[model.__table__ for model in models]))
        db.add(User(id="human", org_id=ORG, team_id="team", email="reviewer@example.test", cognito_sub="sub"))
        db.add(
            PersonaModelPreference(
                org_id=ORG,
                principal_kind="human",
                principal_source="self",
                principal_id="human",
                persona_key="agent-codex-reviewer",
                canonical_model_id=ASTRA,
                revision=2,
                updated_by="human",
                updated_by_source="self",
            )
        )
        await db.commit()
    monkeypatch.setenv("PERSONA_MODEL_MAPPING_ENABLED", "true")
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.resolve_launch_configuration", resolve_launch_configuration)
    contracts, _ = _contract_maps()
    parent_snapshot = live_snapshot(
        tenant_id=ORG,
        principal_id="human",
        correlation_id=ctx.flow.id,
        root_invocation_id=ctx.root,
        mappings={"agent-codex-reviewer": ASTRA},
        persona_contracts=contracts,
        class_defaults={"codex-sdk": {"model_id": ASTRA, "revision": 1, "posture": "report_only", "posture_revision": 1}},
    )
    parent = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{ctx.root}")
    parent["status"] = {"S": "pending"}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=parent)
    digest = await _persist_snapshot(store=ctx.store, invocation_id=ctx.root, tenant_id=ORG, snapshot=parent_snapshot)
    await ctx.finish(ctx.root)
    return digest


async def failed(ctx):
    await tick(ctx)
    run = ctx.calls[-1]["message_id"]
    await review(ctx, findings=[{"finding_id": "ci", "summary": "Final-head CI pending"}])
    raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
    raw["terminal_outcome"] = {"S": "failed"}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    await tick(ctx)
    assert (await state(ctx))[0].block_detail == "reviewer_delivery_blocked"
    req = ReviewRecoveryRequest(
        expected_attempt=1,
        expected_plan_version=1,
        expected_run_id=run,
        expected_head_sha=ctx.head,
        reason="Owner authorizes fresh review of retained work after CI completes.",
    )
    return req, raw


async def call(ctx, req, *, accept=False, actor_id="human"):
    async with ctx.factory() as db:
        value = await request_recovery(
            db, org_id=ORG, node_id=ctx.node.id, actor_id=actor_id, actor_role="owner", request=req, accept=accept, services=ctx.service
        )
        if accept:
            await db.commit()
        return value


async def preserving_amendment(ctx, monkeypatch):
    """Persist an ordinary human amendment while retaining the admitted story."""
    from src.orchestration import review_cycle_dispatch
    from src.orchestration.continuation import digest

    async with ctx.factory() as db:
        base = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        # The cycle fixture stores only policy; include the graph used by the
        # production lineage resolver instead of mocking preservation as true.
        document = {
            **base.plan_document,
            "nodes": [{"address": "cycle/E1/W1/N1", "kind": "story", "issue_ref": "43", "title": "Bound story"}],
            "edges": [],
        }
        base.plan_document = document
        base.accepted_by_decision_id = ctx.approval.id
        base.superseded_at = datetime.now(UTC)
        amendment = OrchestrationDecision(
            org_id=ORG, flow_id=ctx.flow.id, kind="plan_amended", actor_kind="human", actor_id="human", actor_role="owner"
        )
        db.add(amendment)
        await db.flush()
        updated = {**document, "nodes": [*document["nodes"], {"address": "cycle/E1/W2/G1", "kind": "gate"}]}
        plan = OrchestrationAcceptedPlan(
            org_id=ORG,
            flow_id=ctx.flow.id,
            version=2,
            plan_document=updated,
            plan_hash=digest(updated),
            accepted_by_decision_id=amendment.id,
        )
        db.add(plan)
        await db.commit()
    original = review_cycle_dispatch.resolve_authorization_context

    async def authorization(db, **kwargs):
        result = await original(db, **kwargs)
        return replace(result, accepted_plan_version=kwargs["plan_version"], in_force_plan_version=kwargs["plan_version"])

    monkeypatch.setattr(review_cycle_dispatch, "resolve_authorization_context", authorization)
    return plan


async def test_owner_recovery_after_preserving_amendment_keeps_assignment_and_attempts(cycle, monkeypatch):
    req, raw = await failed(cycle)
    before, claim_before, node_before, actions_before = await state(cycle)
    await preserving_amendment(cycle, monkeypatch)
    req.expected_plan_version = 2
    preview = await call(cycle, req)
    assert preview["plan_version"] == 2 and cycle.identity.accepted_plan_version == 1
    req.expected_snapshot = preview["snapshot"]
    accepted = await call(cycle, req, accept=True)
    after_accept = await state(cycle)
    assert after_accept[0].attempts == before.attempts
    assert after_accept[1].generation == claim_before.generation
    assert len(after_accept[3]) == len(actions_before)
    assert (await tick(cycle)).effects_succeeded == 1, (await state(cycle))[0].block_detail
    execution, claim, node, actions = await state(cycle)
    assert execution.id == before.id and execution.accepted_plan_version == 1
    assert node.attempts == node_before.attempts == 1
    assert execution.attempts == before.attempts + 1
    assert len(actions) == len(actions_before) + 1
    assert claim.id == claim_before.id and claim.active_run_id != req.expected_run_id
    assert actions[-1].detail["protected_recovery_decision_id"] == accepted["decision_id"]
    assert cycle.calls[-1]["review_cycle_input"]["remaining_attempts"] == 7
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{req.expected_run_id}") == raw
    assert (await tick(cycle)).effects_attempted == 0
    assert len(cycle.calls) == 2


@pytest.mark.parametrize("change", ["definition", "prerequisite", "amendment_actor"])
async def test_recovery_verification_requires_node_preserving_lineage(cycle, monkeypatch, change):
    from src.orchestration.execution_runner import RunnerContext

    req, _ = await failed(cycle)
    plan = await preserving_amendment(cycle, monkeypatch)
    req.expected_plan_version = 2
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    accepted = await call(cycle, req, accept=True)
    execution, _, _, _ = await state(cycle)
    async with cycle.factory() as db:
        current = await db.get(OrchestrationAcceptedPlan, plan.id)
        document = json.loads(json.dumps(current.plan_document))
        if change == "definition":
            document["nodes"][0]["criteria"] = ["changed execution requirement"]
        elif change == "prerequisite":
            document["edges"] = [{"from_address": "cycle/E1/W2/G1", "to_address": "cycle/E1/W1/N1"}]
        else:
            amendment = OrchestrationDecision(
                org_id=ORG, flow_id=cycle.flow.id, kind="plan_amended", actor_kind="service", actor_id="service", actor_role="owner"
            )
            db.add(amendment)
            await db.flush()
            current.accepted_by_decision_id = amendment.id
        current.plan_document = document
        await db.flush()
        with pytest.raises(CycleBlockedError, match="recovery_authority_changed"):
            await verified_decision(
                db,
                decision_id=accepted["decision_id"],
                context=RunnerContext(cycle.identity, execution, datetime.now(UTC)),
                node=cycle.node,
                binding=cycle.binding,
                services=cycle.service,
                prior_run_id=req.expected_run_id,
                head_sha=cycle.head,
            )
    assert len(cycle.calls) == 1


async def test_owner_recovery_restarts_review_without_accepting_code_or_resetting_attempts(cycle):
    req, raw = await failed(cycle)
    preview = await call(cycle, req)
    assert preview["next_action"] == "fresh_codex_review" and len(cycle.calls) == 1
    req.expected_snapshot = preview["snapshot"]
    accepted = await call(cycle, req, accept=True)
    assert accepted["created"] is True
    assert (await call(cycle, req, accept=True))["created"] is False
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{req.expected_run_id}") == raw
    assert (await tick(cycle)).effects_succeeded == 1, (await state(cycle))[0].block_detail
    execution, claim, node, actions = await state(cycle)
    assert len(cycle.calls) == 2 and claim.active_run_id != req.expected_run_id
    assert node.attempts == 1 and execution.phase == "awaiting_review" and execution.status != "concluded"
    assert actions[-1].detail["protected_recovery_decision_id"] == accepted["decision_id"]
    assert cycle.calls[-1]["review_expect"]["author_run_id"] == cycle.root
    assert cycle.calls[-1]["review_cycle_input"]["remaining_attempts"] == 7
    await review(cycle, findings=[{"finding_id": "still-blocked", "summary": "Real unresolved finding"}])
    await tick(cycle)
    assert len(cycle.calls) == 2


@pytest.mark.parametrize("change", ["head", "active", "revoked", "plan", "binding", "actor", "terminal"])
async def test_recovery_refuses_changed_preview_or_missing_authority(cycle, change, monkeypatch):
    req, raw = await failed(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    actor = "human"
    if change == "head":
        cycle.head = "d" * 40
    elif change in {"active", "revoked", "terminal"}:
        raw["terminal_outcome" if change == "terminal" else "status"] = {"S": "complete" if change == "terminal" else change}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    elif change == "plan":
        req.expected_plan_version = 2
    elif change == "binding":
        from src.orchestration.models import OrchestrationPullRequestBinding

        async with cycle.factory() as db:
            binding = await db.get(OrchestrationPullRequestBinding, cycle.binding.id)
            binding.revision += 1
            await db.commit()
    elif change == "actor":
        actor = "other-owner"
        from unittest.mock import AsyncMock

        monkeypatch.setattr("src.orchestration.protected_review_recovery.policy_owner_matches", AsyncMock(return_value=False))
    with pytest.raises(CycleBlockedError):
        await call(cycle, req, accept=True, actor_id=actor)
    async with cycle.factory() as db:
        assert not list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == KIND)))
    assert len(cycle.calls) == 1


async def test_recovery_rechecks_head_before_dispatch(cycle):
    req, _ = await failed(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    cycle.head = "e" * 40
    assert (await tick(cycle)).effects_attempted == 0
    assert len(cycle.calls) == 1


async def test_owner_recovery_after_retired_intent_retains_full_attempt_history(cycle):
    from src.orchestration.unstarted_review import retired_unstarted_dispatch
    from tests.orchestration.test_review_cycle import unstarted_intent

    req, raw = await failed(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    await unstarted_intent(cycle)
    cycle.head = "e" * 40
    assert (await tick(cycle)).errors == 0
    assert retired_unstarted_dispatch((await state(cycle))[3][-1])
    req.expected_head_sha = cycle.head
    req.expected_snapshot = None
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    assert (await tick(cycle)).effects_succeeded == 1
    execution, claim, node, actions = await state(cycle)
    assert len(actions) == 3  # original intent, retired intent, fresh intent
    assert actions[-1].detail["sequence"] == 3
    assert cycle.calls[-1]["review_cycle_input"]["remaining_attempts"] == 6
    assert node.attempts == 1 and claim.generation == 5 and execution.phase == "awaiting_review"
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{req.expected_run_id}") == raw


async def test_existing_recovery_entrypoint_selects_protected_plan(cycle, monkeypatch):
    from src.orchestration.review_recovery import request_review_recovery

    req, _ = await failed(cycle)
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.get_engine_authority_writer", lambda: cycle.service.writer)
    async with cycle.factory() as db:
        preview = await request_review_recovery(db, org_id=ORG, node_id=cycle.node.id, actor_id="human", actor_role="owner", request=req)
        req.expected_snapshot = preview["snapshot"]
        accepted = await request_review_recovery(
            db, org_id=ORG, node_id=cycle.node.id, actor_id="human", actor_role="owner", request=req, accept=True
        )
        await db.commit()
    assert accepted["contract"] == "protected-review-recovery/v1"
    assert (await tick(cycle)).effects_succeeded == 1


@pytest.mark.parametrize("change", ["revoked", "budget"])
async def test_recovery_does_not_override_later_revocation_or_spend_limit(cycle, change):
    from decimal import Decimal

    req, raw = await failed(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    if change == "revoked":
        raw["status"] = {"S": "revoked"}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    else:
        cycle.spend = Decimal(100)
    assert (await tick(cycle)).effects_attempted == 0
    assert len(cycle.calls) == 1


async def test_explicit_owner_recovery_can_release_budget_hold_without_resetting_attempts(cycle):
    req, raw = await failed(cycle)
    raw["terminal_outcome"] = {"S": "budget_stopped"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    accepted = await call(cycle, req, accept=True)
    assert (await tick(cycle)).effects_succeeded == 1, (await state(cycle))[0].block_detail
    execution, claim, node, actions = await state(cycle)
    assert len(cycle.calls) == 2 and node.attempts == 1
    assert actions[-1].detail["protected_recovery_decision_id"] == accepted["decision_id"]
    assert cycle.calls[-1]["review_cycle_input"]["remaining_attempts"] == 7
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{req.expected_run_id}") == raw


@pytest.mark.parametrize("cycle", [{"claim_generation": 1, "max_attempts": 3}], indirect=True)
async def test_scoped_model_recovery_seals_real_selection_and_preserves_snapshot_claim_budget_and_attempts(cycle, monkeypatch):
    from src.agentauth.model_policy import _resolve_execution_decision
    from src.shared.models.persona_models import PersonaModelPreference

    snapshot_digest = await configured_reviewer(cycle, monkeypatch)
    req, raw = await failed(cycle)
    assert cycle.calls[-1]["model_resolved"] == ASTRA
    raw["terminal_outcome"] = {"S": "budget_stopped"}
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    req.reviewer_model = "gpt6-sol"
    parent_before = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")
    before, claim_before, _, _ = await state(cycle)
    preview = await call(cycle, req)
    selected = preview["reviewer_model_configuration"]
    assert selected["model_requested"] == "gpt6-sol" and selected["model_resolved"] == SOL
    assert selected["model_selection"]["principal_id"] == "human"
    assert selected["model_selection"]["harness_contract_revision"] == "0.155.1"
    req.expected_snapshot = preview["snapshot"]
    accepted = await call(cycle, req, accept=True)
    assert (await state(cycle))[0].attempts == before.attempts
    assert (await tick(cycle)).effects_succeeded == 1
    recovered = cycle.calls[-1]
    assert all(recovered[key] == value for key, value in selected.items())
    child = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{recovered['message_id']}")
    assert child["direct_model_requested"] == {"S": "gpt6-sol"}
    assert child["direct_model_override"] == {"S": SOL}
    assert child["model_policy_snapshot_digest"] == {"S": snapshot_digest}
    assert child["model_policy_snapshot"] == parent_before["model_policy_snapshot"]
    _, decision = _resolve_execution_decision(raw_execution=child, record=SimpleNamespace(tenant_id=ORG, invocation_id=recovered["message_id"]))
    assert decision.requested_model_id == "gpt6-sol"
    assert decision.resolved_model_id == SOL and decision.resolution_source == "explicit-direct"
    execution, claim, node, actions = await state(cycle)
    assert execution.id == before.id and execution.attempts == before.attempts + 1 == 2
    assert claim.id == claim_before.id and claim.generation == claim_before.generation == 1
    assert node.attempts == 1 and recovered["review_cycle_input"]["remaining_attempts"] == 2
    assert recovered["correlation"]["parent_principal"] == f"{cycle.root}#1"
    assert actions[-1].detail["protected_recovery_decision_id"] == accepted["decision_id"]
    assert cycle.spend == 2 and cycle.store._read(f"TENANT#{ORG}", f"EXEC#{req.expected_run_id}") == raw
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}") == parent_before
    async with cycle.factory() as db:
        saved = await db.scalar(select(PersonaModelPreference).where(PersonaModelPreference.persona_key == "agent-codex-reviewer"))
        assert saved.canonical_model_id == ASTRA and saved.revision == 2
        audit = await db.get(OrchestrationDecision, accepted["decision_id"])
        assert json.loads(audit.reason)["reviewer_model_configuration"] == selected
    # Retained legacy work can launch a separate repair; that launch retains
    # the saved preference rather than inheriting this recovery's override.
    await legacy_delivery(cycle)
    await review(cycle, findings=[{"finding_id": "F1", "summary": "A real remaining defect"}])
    assert (await tick(cycle)).effects_succeeded == 1, (await state(cycle))[0].block_detail
    assert cycle.calls[-1]["model_resolved"] == ASTRA
    assert "model_requested" not in cycle.calls[-1]


@pytest.mark.parametrize("change", ["model", "principal", "disallowed", "harness", "claim", "head", "plan"])
async def test_scoped_model_snapshot_rejects_changed_selection_and_authority(cycle, monkeypatch, change):
    from src.admin.persona_models import catalogue_service
    from src.shared.models.organization import User

    await configured_reviewer(cycle, monkeypatch)
    req, _ = await failed(cycle)
    req.reviewer_model = "gpt6-sol"
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    if change == "model":
        req.reviewer_model = "gpt6-luna"
    elif change == "principal":
        async with cycle.factory() as db:
            user = await db.get(User, "human")
            user.org_id = "another-tenant"
            await db.commit()
    elif change == "disallowed":
        from unittest.mock import AsyncMock

        monkeypatch.setattr(
            "src.agentauth.model_policy._resolve_active_allowlist_policy", AsyncMock(return_value=SimpleNamespace(tenant_patterns=[]))
        )
    elif change == "harness":
        original = catalogue_service.validate_selection

        async def changed(*args, **kwargs):
            result = await original(*args, **kwargs)
            return result.model_copy(update={"harness_contract_revision": "future-harness"})

        monkeypatch.setattr(catalogue_service, "validate_selection", changed)
    elif change == "claim":
        from src.orchestration.models import OrchestrationWorkClaim

        async with cycle.factory() as db:
            claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
            claim.generation += 1
            await db.commit()
    elif change == "head":
        cycle.head = "d" * 40
    else:
        req.expected_plan_version += 1
    with pytest.raises(CycleBlockedError):
        await call(cycle, req, accept=True)
    assert len(cycle.calls) == 1


async def test_model_change_on_accepted_snapshot_replay_is_a_conflict(cycle, monkeypatch):
    await configured_reviewer(cycle, monkeypatch)
    req, _ = await failed(cycle)
    req.reviewer_model = "gpt6-sol"
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    assert (await call(cycle, req, accept=True))["created"] is False
    req.reviewer_model = "gpt6-luna"
    with pytest.raises(CycleBlockedError, match="recovery_receipt_conflict"):
        await call(cycle, req, accept=True)


@pytest.mark.parametrize("change", ["restriction", "saved_envelope"])
async def test_saved_dispatch_receipt_revalidates_before_provisioning_or_publication(cycle, monkeypatch, change):
    from unittest.mock import AsyncMock

    from src.orchestration.review_cycle_dispatch import receipt_id

    await configured_reviewer(cycle, monkeypatch)
    req, _ = await failed(cycle)
    req.reviewer_model = "gpt6-sol"
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    provision = Mock(side_effect=RuntimeError("process ended after the SQL receipt"))
    monkeypatch.setattr(cycle.service, "provision", provision)
    await tick(cycle)
    assert provision.call_count == 1 and len(cycle.calls) == 1
    if change == "restriction":
        monkeypatch.setattr(
            "src.agentauth.model_policy._resolve_active_allowlist_policy", AsyncMock(return_value=SimpleNamespace(tenant_patterns=[]))
        )
    else:
        action = (await state(cycle))[3][-1]
        async with cycle.factory() as db:
            receipt = await db.get(OrchestrationDecision, receipt_id(action.operation_key))
            value = json.loads(receipt.reason)
            value["envelope"]["model_resolved"] = ASTRA
            # Simulate isolated stored-receipt corruption without weakening
            # the production append-only ORM guard.
            await db.execute(
                update(OrchestrationDecision.__table__)
                .where(OrchestrationDecision.__table__.c.id == receipt.id)
                .values(reason=json.dumps(value))
                .execution_options(synchronize_session=False)
            )
            await db.commit()
    provision.reset_mock()
    await tick(cycle)
    provision.assert_not_called()
    assert len(cycle.calls) == 1


async def test_scoped_model_rechecks_restrictions_after_provision_before_queue(cycle, monkeypatch):
    from unittest.mock import AsyncMock

    await configured_reviewer(cycle, monkeypatch)
    req, _ = await failed(cycle)
    req.reviewer_model = "gpt6-sol"
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    original = cycle.service.provision

    def provision(*args):
        original(*args)
        monkeypatch.setattr(
            "src.agentauth.model_policy._resolve_active_allowlist_policy", AsyncMock(return_value=SimpleNamespace(tenant_patterns=[]))
        )

    monkeypatch.setattr(cycle.service, "provision", provision)
    await tick(cycle)
    assert len(cycle.calls) == 1


async def test_scoped_model_does_not_bypass_budget_admission(cycle, monkeypatch):
    from src.orchestration.execution_policy import Decision, DenyReason

    await configured_reviewer(cycle, monkeypatch)
    req, _ = await failed(cycle)
    req.reviewer_model = "gpt6-sol"
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    cycle.reserve.return_value = Decision.block(DenyReason.SPEND_LIMIT_EXCEEDED, "existing budget limit")
    await tick(cycle)
    assert len(cycle.calls) == 1 and cycle.spend == 2


async def test_developer_recovery_cannot_use_reviewer_override(cycle):
    req = ReviewRecoveryRequest(
        expected_attempt=1,
        expected_plan_version=1,
        expected_run_id=cycle.root,
        expected_head_sha=cycle.head,
        reason="Recover retained work with a reviewer",
        reviewer_model="gpt6-sol",
    )
    with pytest.raises(CycleBlockedError, match="reviewer_model_override_unsupported"):
        await call(cycle, req)
