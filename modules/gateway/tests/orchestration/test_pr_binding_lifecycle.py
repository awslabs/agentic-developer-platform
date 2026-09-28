"""Binding retries, accepted scope and durable observation regressions (#5301)."""

import json

from sqlalchemy import select

from src.orchestration.dispatch_pass import attempt_run_id
from src.orchestration.models import ActorKind, DecisionKind, OrchestrationDecision
from src.orchestration.pr_bindings import PullRequestIdentity, active_binding_for_node, recover_binding, register_binding, resolve_registration_target
from src.orchestration.results import observe_results
from tests.orchestration import test_story_reconciliation as fixtures
from tests.orchestration.test_story_reconciliation import (
    HEAD,
    INSTALLATION,
    NEW_HEAD,
    ORG_A,
    PR_NODE,
    REPO,
    REPO_ID,
    StubSource,
    _bind,
    _green,
    _story,
)

engine = fixtures.engine
session = fixtures.session


def identity(*, head=HEAD, number=5293, node_id=PR_NODE):
    return PullRequestIdentity(REPO_ID, node_id, REPO, number, head)


class RunStore:
    def __init__(self, node):
        self.node_id, self.org_id, self.attempt = node.id, node.org_id, node.attempts

    def get(self, *args):
        return {"tenant_id": self.org_id, "engine_node_id": self.node_id, "engine_attempt": self.attempt, "status": "complete"}


async def _installation(*args, **kwargs):
    return INSTALLATION


async def observations(session, node):
    rows = (
        (
            await session.execute(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.node_id == node.id,
                    OrchestrationDecision.kind == DecisionKind.RESULT_OBSERVED.value,
                )
                .order_by(OrchestrationDecision.created_at)
            )
        )
        .scalars()
        .all()
    )
    return [json.loads(row.reason) for row in rows]


async def test_retry_reuses_pr_and_retains_previous_head_attempt_and_provenance(session, monkeypatch):
    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _installation)
    node, dispatch = await _story(session, binding_marker=True)
    original = await _bind(session, node, dispatch)
    node.attempts = 2
    second = {**dispatch, "attempt": 2, "run_id": attempt_run_id(node.id, 2)}
    session.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="engine",
            actor_role="service",
            actor_kind="service",
            reason=json.dumps(second),
        )
    )
    await session.flush()
    target = await resolve_registration_target(session, run_id=second["run_id"])
    current, created = await register_binding(
        session, target=target, pr=identity(head=NEW_HEAD), actor_id="replacement-worker", actor_kind=ActorKind.SERVICE
    )
    assert current.id == original.id and not created
    assert (await active_binding_for_node(session, org_id=ORG_A, node_id=node.id, attempt=2)).id == current.id
    history = (
        (
            await session.execute(
                select(OrchestrationDecision)
                .where(
                    OrchestrationDecision.node_id == node.id,
                    OrchestrationDecision.kind == DecisionKind.PR_BINDING_CHANGED.value,
                )
                .order_by(OrchestrationDecision.created_at)
            )
        )
        .scalars()
        .all()
    )
    snapshots = [json.loads(row.reason) for row in history]
    assert [(row["attempt"], row["head_sha"], row["registered_by"]) for row in snapshots] == [
        (1, HEAD, "scaledjob-worker"),
        (2, NEW_HEAD, "replacement-worker"),
    ]
    await register_binding(session, target=target, pr=identity(head=NEW_HEAD), actor_id="replacement-worker", actor_kind=ActorKind.SERVICE)
    assert current.revision == 2


async def test_operator_replacement_across_attempts_supersedes_old_pr(session, monkeypatch):
    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _installation)
    node, dispatch = await _story(session, binding_marker=True)
    old = await _bind(session, node, dispatch)
    current = await recover_binding(
        session,
        org_id=ORG_A,
        node_id=node.id,
        pr=identity(number=5294, node_id="PR_replacement"),
        installation_id=INSTALLATION,
        actor_id="operator",
        reason="verified replacement scope",
        replaces_reason="old implementation abandoned",
    )
    assert old.state == "superseded" and current.state == "active"
    assert current.registered_by == "operator"
    assert (await active_binding_for_node(session, org_id=ORG_A, node_id=node.id, attempt=1)).id == current.id


async def test_observer_persists_changing_holds_and_one_verified_receipt(session, monkeypatch):
    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _installation)
    monkeypatch.setattr("src.orchestration.results.resolve_installation_id", _installation)
    node, dispatch = await _story(session, binding_marker=True)
    store = RunStore(node)
    source = StubSource(evidence=_green())
    first = await observe_results(session, run_store=store, evidence=source)
    assert first.waiting == 1 and len(await observations(session, node)) == 1
    await observe_results(session, run_store=store, evidence=source)
    assert len(await observations(session, node)) == 1
    binding = await _bind(session, node, dispatch)
    source.evidence = _green(review_approved=False)
    await observe_results(session, run_store=store, evidence=source)
    held = await observations(session, node)
    assert len(held) == 2 and "no verified approval for its current head" in held[-1]["evidence"]
    source.evidence = _green()
    passed = await observe_results(session, run_store=store, evidence=source)
    assert passed.advanced == 1 and node.state == "passed"
    await observe_results(session, run_store=store, evidence=source)
    completed = await observations(session, node)
    assert len(completed) == 3
    receipt = completed[-1]
    assert receipt["binding"]["id"] == binding.id
    assert receipt["binding"]["accepted_scope"]
    assert receipt["merge_receipt"]["head_sha"] == HEAD
    assert receipt["merge_receipt"]["merge_commit_sha"] == HEAD
    assert receipt["merge_receipt"]["provider_pr_node_id"] == PR_NODE


async def test_changed_scope_holds_until_attributed_recovery(session, monkeypatch):
    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _installation)
    monkeypatch.setattr("src.orchestration.results.resolve_installation_id", _installation)
    node, dispatch = await _story(session, binding_marker=True)
    binding = await _bind(session, node, dispatch)
    original_scope = binding.accepted_scope
    node.title = "Newly accepted implementation scope"
    await session.flush()
    store, source = RunStore(node), StubSource(evidence=_green())
    report = await observe_results(session, run_store=store, evidence=source)
    assert report.advanced == 0 and "scope changed" in report.reasons[node.id]
    await recover_binding(
        session,
        org_id=ORG_A,
        node_id=node.id,
        pr=identity(),
        installation_id=INSTALLATION,
        actor_id="operator",
        reason="verified implementation covers newly accepted scope",
    )
    assert binding.accepted_scope != original_scope
    report = await observe_results(session, run_store=store, evidence=source)
    assert report.advanced == 1


async def test_reviewer_retry_downgrades_implementation_and_service_cannot_upgrade(session, monkeypatch):
    from src.orchestration.models import BindingRole

    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _installation)
    node, dispatch = await _story(session, binding_marker=True)
    binding = await _bind(session, node, dispatch)
    node.attempts = 2
    second = {**dispatch, "attempt": 2, "run_id": attempt_run_id(node.id, 2)}
    session.add(
        OrchestrationDecision(
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="engine",
            actor_role="service",
            actor_kind="service",
            reason=json.dumps(second),
        )
    )
    await session.flush()
    target = await resolve_registration_target(session, run_id=second["run_id"])
    await register_binding(
        session, target=target, pr=identity(), actor_id="reviewer", actor_kind=ActorKind.SERVICE, declared_role=BindingRole.REVIEWER_ARTIFACT
    )
    assert binding.attempt == 2 and binding.role == BindingRole.REVIEWER_ARTIFACT.value
    await register_binding(
        session, target=target, pr=identity(), actor_id="worker", actor_kind=ActorKind.SERVICE, declared_role=BindingRole.IMPLEMENTATION
    )
    assert binding.role == BindingRole.REVIEWER_ARTIFACT.value


async def test_binding_snapshots_dispatch_authority_and_accepted_plan(session, monkeypatch):
    from src.orchestration.models import OrchestrationAcceptedPlan

    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _installation)
    node, dispatch = await _story(session, binding_marker=True)
    plan = OrchestrationAcceptedPlan(
        org_id=ORG_A,
        flow_id=node.flow_id,
        version=1,
        plan_hash="a" * 64,
        accepted_by_decision_id="accepted-root",
        plan_document={
            "nodes": [
                {
                    "address": "flow-5049/epic-1/wave-1/story-5049",
                    "kind": node.kind,
                    "issue_ref": node.issue_ref,
                    "title": node.title,
                }
            ]
        },
    )
    session.add(plan)
    await session.flush()
    authorized_dispatch = {**dispatch, "root_decision_id": "accepted-root"}
    session.add(
        OrchestrationDecision(
            org_id=ORG_A,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=DecisionKind.NODE_DISPATCHED.value,
            actor_id="engine",
            actor_role="service",
            actor_kind="service",
            reason=json.dumps(authorized_dispatch),
        )
    )
    await session.flush()
    binding = await _bind(session, node, authorized_dispatch)
    scope = json.loads(binding.accepted_scope)
    assert scope["accepted_plan_id"] == plan.id
    assert scope["accepted_plan_hash"] == plan.plan_hash
    assert scope["root_decision_id"] == "accepted-root"
    assert scope["dispatch_decision_id"]


async def test_admitted_repairs_carry_same_pr_with_new_revision_and_fence_old_worker(session, monkeypatch):
    import pytest

    from src.orchestration.pr_bindings import BindingError, BindingRefusal, carry_forward_binding

    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _installation)
    node, dispatch = await _story(session, binding_marker=True)
    original = await _bind(session, node, dispatch)
    original_id = original.id
    old_target = await resolve_registration_target(session, run_id=dispatch["run_id"])
    for attempt in (2, 3, 4):
        node.attempts = attempt
        current_dispatch = {**dispatch, "attempt": attempt, "run_id": attempt_run_id(node.id, attempt)}
        session.add(
            OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind=DecisionKind.NODE_DISPATCHED.value,
                actor_id="engine",
                actor_role="service",
                actor_kind="service",
                reason=json.dumps(current_dispatch),
            )
        )
        await session.flush()
        target = await resolve_registration_target(session, run_id=current_dispatch["run_id"])
        current = await carry_forward_binding(
            session,
            node=node,
            previous_attempt=attempt - 1,
            target=target,
            pr=identity(head=f"{attempt:040x}"),
            expected_revision=attempt - 1,
        )
        assert current.id == original_id and current.attempt == attempt and current.revision == attempt
        assert (await active_binding_for_node(session, org_id=ORG_A, node_id=node.id, attempt=attempt)).id == original_id
        assert await active_binding_for_node(session, org_id=ORG_A, node_id=node.id, attempt=attempt - 1) is None
        with pytest.raises(BindingError) as stale:
            await register_binding(session, target=old_target, pr=identity(), actor_id="late-worker", actor_kind=ActorKind.SERVICE)
        assert stale.value.code == BindingRefusal.STALE_RUN
    snapshots = (
        await session.scalars(
            select(OrchestrationDecision).where(
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.kind == DecisionKind.PR_BINDING_CHANGED.value,
            )
        )
    ).all()
    assert sorted(json.loads(row.reason)["attempt"] for row in snapshots) == [1, 2, 3, 4]
    assert node.attempts == 4  # Carrying a binding never refunds consumed attempts.


async def test_repair_carry_refuses_scope_revision_and_repository_change(session, monkeypatch):
    from dataclasses import replace

    import pytest

    from src.orchestration.pr_bindings import BindingError, carry_forward_binding

    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _installation)
    node, dispatch = await _story(session, binding_marker=True)
    binding = await _bind(session, node, dispatch)
    target = await resolve_registration_target(session, run_id=dispatch["run_id"])
    node.attempts = 2
    await session.flush()
    target = replace(target, attempt=2, run_id=attempt_run_id(node.id, 2))
    for override in ({"expected_revision": 999}, {"pr": identity(node_id="other-pr")}):
        arguments = dict(node=node, previous_attempt=1, target=target, pr=identity(), expected_revision=1)
        arguments.update(override)
        with pytest.raises(BindingError):
            await carry_forward_binding(session, **arguments)
    node.title = "Changed accepted scope"
    with pytest.raises(BindingError, match="scope changed"):
        await carry_forward_binding(session, node=node, previous_attempt=1, target=target, pr=identity(), expected_revision=1)
    assert binding.attempt == 1 and binding.revision == 1


async def test_authenticated_repair_run_in_same_attempt_gets_new_revision_even_without_push(session, monkeypatch):
    from dataclasses import replace

    monkeypatch.setattr("src.orchestration.pr_bindings.resolve_installation_id", _installation)
    node, dispatch = await _story(session, binding_marker=True)
    binding = await _bind(session, node, dispatch)
    target = await resolve_registration_target(session, run_id=dispatch["run_id"])
    # The run-report adapter authenticates this K2 assignment before constructing
    # its target. The binding store does not invent a new story attempt for it.
    repair = replace(target, run_id="continuation:repair-1")
    current, created = await register_binding(
        session,
        target=repair,
        pr=identity(),
        actor_id="repair-worker",
        actor_kind=ActorKind.SERVICE,
    )
    assert not created and current.id == binding.id and current.attempt == node.attempts == 1
    assert current.run_id == repair.run_id and current.revision == 2
    await register_binding(session, target=repair, pr=identity(), actor_id="repair-worker", actor_kind=ActorKind.SERVICE)
    assert current.revision == 2
    rows = (
        await session.scalars(
            select(OrchestrationDecision).where(
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.kind == DecisionKind.PR_BINDING_CHANGED.value,
            )
        )
    ).all()
    assert {json.loads(row.reason)["run_id"] for row in rows} == {dispatch["run_id"], repair.run_id}
