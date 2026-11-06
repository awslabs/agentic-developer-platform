"""Archive transitions keep a durable, attributed record without changing progress."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from src.orchestration.execution_policy import Action, ExecutionPolicy, PolicyLimits, stamp_policy
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationNode
from tests.orchestration import test_flow_archive_authorization as authorization_fixtures
from tests.orchestration import test_flow_controls as fixtures
from tests.orchestration.test_controls import ORG_A, seed_flow

session, controls = fixtures.session, fixtures.controls
app_with_router, authorized_app = authorization_fixtures.app_with_router, authorization_fixtures.authorized_app


async def test_archive_rejected_flow_records_decision(session, controls):
    client, _, access, _ = controls
    flow = await seed_flow(session)
    session.add(
        OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="epic",
            wave_ref="wave",
            node_ref="gate",
            kind="gate",
            title="Rejected plan",
            state="rejected_at_gate",
        )
    )
    await session.commit()

    route = f"/orchestration/flows/{flow.id}/archive"
    response = await client.post(route, json={"reason": "The initial plan was rejected"})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == flow.id
    assert response.json()["status"] == "archived"
    archived_at = datetime.fromisoformat(response.json()["archived_at"])
    assert archived_at.tzinfo is not None
    assert archived_at <= datetime.now(UTC)

    await session.refresh(flow)
    assert flow.archived_at is not None
    assert flow.execution_paused is True
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id)))
    assert [(decision.kind, decision.reason) for decision in decisions] == [("flow_archived", "The initial plan was rejected")]
    assert decisions[0].actor_id == "human-1"
    assert decisions[0].actor_kind == "human"
    assert decisions[0].actor_role == "org_admin"

    second = await client.post(route, json={"reason": "The initial plan was rejected"})
    assert second.status_code == 200, second.text
    assert second.json()["archived_at"] == response.json()["archived_at"]
    assert len(list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id)))) == 1
    access.check_permission.assert_awaited()


@pytest.mark.parametrize(
    ("paused", "node_state", "expected_status"),
    [(True, "rejected_at_gate", "attention_needed"), (False, "passed", "complete")],
)
async def test_unarchive_restores_derived_status_without_renewing_authority(session, controls, paused, node_state, expected_status):
    client, _, access, _ = controls
    flow = await seed_flow(session)
    flow.execution_paused = paused
    node = OrchestrationNode(
        org_id=ORG_A,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="step",
        kind="story",
        title="Prior work",
        state=node_state,
        attempts=3,
    )
    expiry = datetime(2020, 1, 1, tzinfo=UTC)
    policy = stamp_policy(
        ExecutionPolicy(
            org_id=ORG_A,
            repository_ids=["example.org/project"],
            allowed_actions=[Action.DEVELOP],
            expires_at=expiry,
            limits=PolicyLimits(
                max_wall_clock_seconds=3600,
                max_spend_usd=Decimal("50"),
                max_attempts_per_node=3,
                max_concurrent_actions=2,
            ),
        ),
        principal_id="human-1",
        org_id=ORG_A,
    )
    document = {"execution_policy": policy.model_dump(mode="json")}
    plan = OrchestrationAcceptedPlan(org_id=ORG_A, flow_id=flow.id, version=1, plan_hash="f" * 64, plan_document=document)
    session.add_all([node, plan])
    await session.commit()

    archive = await client.post(f"/orchestration/flows/{flow.id}/archive", json={"reason": "The prior work can be hidden"})
    assert archive.status_code == 200, archive.text
    assert archive.json()["status"] == "archived"
    assert archive.json()["execution_paused"] is paused

    route = f"/orchestration/flows/{flow.id}/unarchive"
    response = await client.post(route, json={"reason": "Review prior work again"})
    assert response.status_code == 200, response.text
    assert response.json()["id"] == flow.id
    assert response.json()["archived_at"] is None
    assert response.json()["status"] == expected_status
    assert response.json()["execution_paused"] is paused

    second = await client.post(route, json={"reason": "Review prior work again"})
    assert second.status_code == 200, second.text
    assert second.json()["status"] == expected_status
    assert second.json()["archived_at"] is None
    await session.refresh(flow)
    await session.refresh(node)
    await session.refresh(plan)
    assert flow.archived_at is None
    assert flow.execution_paused is paused
    assert (node.state, node.attempts) == (node_state, 3)
    assert plan.version == 1
    assert plan.plan_hash == "f" * 64
    assert plan.plan_document == document
    assert ExecutionPolicy.model_validate(plan.plan_document["execution_policy"]).expires_at == expiry
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id)))
    assert [(decision.kind, decision.reason) for decision in decisions] == [
        ("flow_archived", "The prior work can be hidden"),
        ("flow_unarchived", "Review prior work again"),
    ]
    assert all(decision.actor_id == "human-1" and decision.actor_kind == "human" and decision.actor_role == "org_admin" for decision in decisions)
    assert access.check_permission.await_count == 3


async def test_archive_round_trip(session, authorized_app):
    from fastapi.testclient import TestClient

    from src.admin.config import AdminRole
    from src.orchestration.controls import router as decision_router

    app, role, _ = authorized_app
    role.return_value = (AdminRole.ORG_ADMIN, ORG_A, None)
    app.include_router(decision_router)
    flow = await seed_flow(session)
    session.add(
        OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="epic",
            wave_ref="wave",
            node_ref="gate",
            kind="gate",
            title="Initial plan declined",
            state="rejected_at_gate",
        )
    )
    await session.commit()
    path = f"/orchestration/flows/{flow.id}"
    archive_reason = "The initial plan was superseded"
    unarchive_reason = "Reopen the original plan history"

    with TestClient(app) as client:
        initial = client.get("/orchestration/flows")
        assert initial.status_code == 200, initial.text
        assert [(row["id"], row["status"]) for row in initial.json()["flows"]] == [(flow.id, "attention_needed")]

        archived = client.post(f"{path}/archive", json={"reason": archive_reason})
        assert archived.status_code == 200, archived.text
        assert archived.json()["id"] == flow.id
        assert archived.json()["status"] == "archived"
        assert archived.json()["archived_at"] is not None
        hidden = client.get("/orchestration/flows")
        assert hidden.status_code == 200, hidden.text
        assert hidden.json()["flows"] == []
        included = client.get("/orchestration/flows?include_archived=true")
        assert included.status_code == 200, included.text
        assert included.json()["flows"][0]["archived_at"] == archived.json()["archived_at"]
        graph = client.get(path)
        assert graph.status_code == 200, graph.text
        assert graph.json()["archived_at"] == archived.json()["archived_at"]
        history = client.get(f"{path}/decisions")
        assert history.status_code == 200, history.text
        assert [(entry["kind"], entry["reason"], entry["actor_id"]) for entry in history.json()] == [("flow_archived", archive_reason, "human-1")]

        restored = client.post(f"{path}/unarchive", json={"reason": unarchive_reason})
        assert restored.status_code == 200, restored.text
        assert restored.json()["id"] == flow.id
        assert restored.json()["archived_at"] is None
        assert restored.json()["status"] == initial.json()["flows"][0]["status"]
        visible = client.get("/orchestration/flows")
        assert visible.status_code == 200, visible.text
        assert visible.json()["flows"][0]["status"] == restored.json()["status"]
        graph = client.get(path)
        assert graph.status_code == 200, graph.text
        assert graph.json()["archived_at"] is None
        history = client.get(f"{path}/decisions")
        assert history.status_code == 200, history.text
        assert [(entry["kind"], entry["reason"], entry["actor_id"]) for entry in history.json()] == [
            ("flow_archived", archive_reason, "human-1"),
            ("flow_unarchived", unarchive_reason, "human-1"),
        ]
    await session.refresh(flow)
    assert flow.archived_at is None
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id)))
    assert len(decisions) == 2


async def test_archive_refusals(session, authorized_app):
    from fastapi.testclient import TestClient

    from src.admin.config import AdminRole
    from src.orchestration.models import DecisionKind

    app, role, _ = authorized_app
    role.return_value = (AdminRole.ORG_ADMIN, ORG_A, None)
    flow = await seed_flow(session)
    gate = OrchestrationNode(
        org_id=ORG_A,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="gate",
        kind="gate",
        title="Plan declined",
        state="rejected_at_gate",
    )
    running = OrchestrationNode(
        org_id=ORG_A,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="story",
        kind="story",
        title="Still running",
        state="running",
        attempts=2,
    )
    session.add_all([gate, running])
    await session.flush()
    previous = OrchestrationDecision(
        org_id=ORG_A,
        flow_id=flow.id,
        node_id=gate.id,
        kind=DecisionKind.GATE_REJECTED.value,
        actor_id="human-1",
        actor_kind="human",
        actor_role="org_admin",
        reason="Plan needs a correction",
    )
    session.add(previous)
    await session.commit()

    route = f"/orchestration/flows/{flow.id}/archive"
    with TestClient(app) as client:
        active = client.post(route, json={"reason": "No active work remains"})
        assert active.status_code == 409, active.text
        assert active.json()["detail"]["code"] == "flow_has_active_work"
        role.return_value = (AdminRole.MEMBER, ORG_A, None)
        member = client.post(route, json={"reason": "No active work remains"})
        assert member.status_code == 409, member.text
        assert member.json()["detail"]["code"] == "flow_has_active_work"
        listed = client.get("/orchestration/flows")
        assert listed.status_code == 200, listed.text
        assert [row["id"] for row in listed.json()["flows"]] == [flow.id]
        assert listed.json()["status_counts"]["archived"] == 0

    await session.refresh(flow)
    await session.refresh(gate)
    await session.refresh(running)
    await session.refresh(previous)
    assert flow.archived_at is None
    assert (gate.state, running.state, running.attempts) == ("rejected_at_gate", "running", 2)
    decisions = list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id)))
    assert [(row.id, row.kind, row.reason) for row in decisions] == [(previous.id, DecisionKind.GATE_REJECTED.value, "Plan needs a correction")]


@pytest.mark.parametrize("state", ["running", "awaiting_merge", "awaiting_gate"])
async def test_archive_refuses_active_node_without_writing(session, controls, state):
    client, _, _, _ = controls
    flow = await seed_flow(session)
    session.add(
        OrchestrationNode(
            org_id=ORG_A,
            flow_id=flow.id,
            epic_ref="epic",
            wave_ref="wave",
            node_ref="node",
            kind="story",
            title="Still active",
            state=state,
        )
    )
    await session.commit()
    response = await client.post(f"/orchestration/flows/{flow.id}/archive", json={"reason": "Hide this active work"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "flow_has_active_work"
    await session.refresh(flow)
    assert flow.archived_at is None
    assert list(await session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.flow_id == flow.id))) == []
