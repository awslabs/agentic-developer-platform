"""Base Project defaults, authorization, shared allowances and durable accounting."""

import asyncio
import base64
from dataclasses import replace
from decimal import Decimal

import fakeredis.aioredis
import pytest
from sqlalchemy import select

from src.auth.dependencies import get_current_user
from src.budget.enforcement_service import budget_enforcement_service
from src.budget.project_budget import PROJECT_EPOCH, prepare_project_budget, project_target, reconcile_project_budget
from src.budget.reservations import ReservationStore
from src.budget.run_binding import RunBinding
from src.budget.settlement import settle_usage
from src.orchestration.models import OrchestrationFlow, OrchestrationNode
from src.products.work_scope import (
    ProjectScopeError,
    assign_flow_project,
    authorize_project_selection,
    bind_run,
    resolve_project,
    resolve_run_project,
)
from src.shared.models.audit import AuditLog
from src.shared.models.budget import BudgetUsage
from src.shared.models.organization import TeamMembership, User
from tests.products.test_i05 import REQUEST, headers


@pytest.fixture
async def meter(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(None, 120, client=client)
    monkeypatch.setattr(budget_enforcement_service, "_get_reservations", lambda: store)
    yield store
    await client.aclose()


@pytest.fixture
async def product(test_client, source_member):
    created = await test_client.post("/v1/products", json=REQUEST, headers={"Idempotency-Key": "project_budget_fixture"})
    assert created.status_code == 201, created.text
    return created.json()


async def test_budget_write_records_cli_reason_without_changing_dashboard_calls(test_client, db_session, source_member, product, meter):
    url = f"/v1/projects/{product['project_id']}/budget"
    changed = await test_client.put(url, json={"budget_usd": "250.00"}, headers={"X-ADP-Budget-Reason": "  Approved limit  "})
    assert changed.status_code == 200, changed.text
    audit = await db_session.scalar(select(AuditLog).where(AuditLog.event_type == "project_budget_changed"))
    assert audit.details["reason"] == "Approved limit"
    assert audit.details["budget_usd"] == "250.00"

    dashboard = await test_client.put(url, json={"budget_usd": "200.00"})
    assert dashboard.status_code == 200, dashboard.text
    audits = (await db_session.scalars(select(AuditLog).where(AuditLog.event_type == "project_budget_changed"))).all()
    assert len(audits) == 2
    assert "reason" not in audits[-1].details


async def test_budget_write_refuses_blank_reason(test_client, db_session, source_member, product, meter):
    response = await test_client.put(
        f"/v1/projects/{product['project_id']}/budget",
        json={"budget_usd": "250.00"},
        headers={"X-ADP-Budget-Reason": "   "},
    )
    assert response.status_code == 422
    assert not (await db_session.scalars(select(AuditLog).where(AuditLog.event_type == "project_budget_changed"))).all()


@pytest.mark.parametrize(
    "reason_headers",
    [
        {"X-ADP-Budget-Reason-Base64": "not base64!"},
        {"X-ADP-Budget-Reason-Base64": base64.b64encode(b"\xff").decode("ascii")},
        {"X-ADP-Budget-Reason-Base64": base64.b64encode(b" ").decode("ascii")},
        {"X-ADP-Budget-Reason-Base64": base64.b64encode(b"x" * 501).decode("ascii")},
        {"X-ADP-Budget-Reason-Base64": "eA==", "X-ADP-Budget-Reason": "another reason"},
    ],
)
async def test_invalid_encoded_reason_cannot_change_budget(test_client, db_session, source_member, product, meter, reason_headers):
    url = f"/v1/projects/{product['project_id']}/budget"
    response = await test_client.put(url, json={"budget_usd": "250.00"}, headers=reason_headers)
    assert response.status_code == 422, response.text
    assert (await test_client.get(url)).json()["budget_usd"] is None
    assert await db_session.scalar(select(AuditLog).where(AuditLog.event_type == "project_budget_changed")) is None


async def test_base_default_and_explicit_project_are_tenant_scoped(test_client, db_session, source_member, product):
    product_id, base_id = product["product_id"], product["project_id"]
    default = await resolve_project(db_session, tenant_id=source_member.org_id, product_id=product_id)
    assert default.project_id == base_id
    listing = (await test_client.get("/v1/products")).json()["items"][0]
    assert listing["base_project_id"] == base_id and listing["projects"][0]["is_base"]
    created = await test_client.post(
        f"/v1/products/{product_id}/projects", json={"mode": "create", "name": "Launch"}, headers=headers(1, "project_budget_explicit")
    )
    assert created.status_code == 201, created.text
    selected = await resolve_project(db_session, tenant_id=source_member.org_id, product_id=product_id, project_id=created.json()["project_id"])
    assert selected.project_id != base_id
    assert (await resolve_project(db_session, tenant_id=source_member.org_id, product_id=product_id)).project_id == base_id
    denied = await test_client.delete(f"/v1/products/{product_id}/projects/{base_id}", headers=headers(2, "base_unlink_attempt"))
    assert denied.status_code == 409 and denied.json()["detail"]["code"] == "base_project_required"
    with pytest.raises(ProjectScopeError):
        await resolve_project(db_session, tenant_id="other", product_id=product_id)
    with pytest.raises(ProjectScopeError):
        await resolve_project(db_session, tenant_id=source_member.org_id, product_id="other", project_id=base_id)


async def test_run_and_session_attribution_cannot_move_on_retry(db_session, product, source_member):
    scope = await resolve_project(db_session, tenant_id=source_member.org_id, product_id=product["product_id"])
    await bind_run(db_session, scope=scope, run_id="session", owner_user_id=source_member.user_id)
    binding = RunBinding(
        run_id="child",
        correlation_id="",
        session_id="session",
        tenant_id=source_member.org_id,
        user_id=source_member.user_id,
        root_human_id=source_member.user_id,
        is_human_rooted=True,
    )
    child = await resolve_run_project(db_session, binding=binding)
    assert child.project_id == scope.project_id and child.run_id == "child"
    assert await resolve_run_project(db_session, binding=binding) == child
    with pytest.raises(ProjectScopeError):
        await bind_run(db_session, scope=scope, run_id="child", owner_user_id="different-owner")
    with pytest.raises(ProjectScopeError):
        await resolve_run_project(db_session, binding=replace(binding, root_human_id="different-owner"))


async def test_owned_chat_defaults_to_base_and_revoked_member_cannot_continue(db_session, product, source_member, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from src.products import work_scope_routes
    from src.products.routes import current_owner
    from src.products.work_scope import attach_session_run

    owner = await current_owner(db_session, source_member)
    owned = AsyncMock(return_value={"messages": []})
    monkeypatch.setattr(work_scope_routes.chat_history, "owned_session", owned)
    result = await work_scope_routes.select_session_project(
        "owned-session", work_scope_routes.SessionProject(product_id=product["product_id"]), db_session, source_member, None
    )
    assert result == {"product_id": product["product_id"], "project_id": product["project_id"]}
    child = await attach_session_run(db_session, tenant_id=source_member.org_id, user_id=owner.id, session_id="owned-session", run_id="paid-run")
    assert child.project_id == product["project_id"] and child.run_id == "paid-run"
    owned.return_value = {"messages": [{"role": "user", "content": "started"}]}
    with pytest.raises(HTTPException) as error:
        await work_scope_routes.select_session_project(
            "used-session", work_scope_routes.SessionProject(product_id=product["product_id"]), db_session, source_member, None
        )
    assert error.value.status_code == 409
    memberships = (await db_session.scalars(select(TeamMembership).where(TeamMembership.user_id == owner.id))).all()
    for membership in memberships:
        await db_session.delete(membership)
    await db_session.commit()
    with pytest.raises(ProjectScopeError):
        await attach_session_run(db_session, tenant_id=source_member.org_id, user_id=owner.id, session_id="owned-session", run_id="paid-run")


async def test_flow_defaults_and_started_flow_cannot_move_budget(db_session, product, source_member):
    flow = OrchestrationFlow(id="flow", org_id=source_member.org_id, slug="flow", title="Flow", product_id=product["product_id"])
    db_session.add(flow)
    await db_session.flush()
    await assign_flow_project(db_session, flow=flow, product_id=flow.product_id)
    assert flow.project_id == product["project_id"]
    node = OrchestrationNode(
        id="node",
        org_id=flow.org_id,
        flow_id=flow.id,
        epic_ref="epic",
        wave_ref="wave",
        node_ref="node",
        kind="story",
        state="running",
        attempts=1,
        title="Node",
    )
    db_session.add(node)
    await db_session.flush()
    from src.orchestration.compile import ProposalRejectedError

    with pytest.raises(ProposalRejectedError, match="cannot change"):
        await assign_flow_project(db_session, flow=flow, product_id=None)


async def test_shared_cap_and_settlement_are_atomic_and_idempotent(test_client, db_session, source_member, product, meter):
    project_id = product["project_id"]
    saved = await test_client.put(f"/v1/projects/{project_id}/budget", json={"budget_usd": "1.00"})
    assert saved.status_code == 200, saved.text
    target = project_target(source_member.org_id, project_id, Decimal("1"))
    outcomes = await asyncio.gather(*(meter.reserve(f"request-{i}", Decimal(".3"), [target]) for i in range(8)))
    admitted = [f"request-{i}" for i, outcome in enumerate(outcomes) if outcome.admitted]
    assert len(admitted) == 3
    assert (await meter.snapshot(target)).total_usd == Decimal(".9")
    request_id = admitted[0]
    await settle_usage(
        db_session,
        org_id=source_member.org_id,
        request_id=request_id,
        user_id=source_member.user_id,
        cost=Decimal(".1"),
        total_tokens=12,
        entities=[("project", project_id)],
        reservation_scope_keys=[target.key()],
    )
    await db_session.commit()
    await reconcile_project_budget(db_session, meter, target)
    await reconcile_project_budget(db_session, meter, target)
    snap = await meter.snapshot(target, include_requests=True)
    assert snap.total_usd == Decimal(".7") and snap.request_amounts["__settled__"] == Decimal(".1")
    assert request_id not in snap.request_amounts
    replay = await meter.reserve(request_id, Decimal(".3"), [target])
    assert replay is not None and not replay.admitted
    assert (await meter.snapshot(target)).total_usd == Decimal(".7")
    assert (await meter.reserve("next", Decimal(".3"), [target])).admitted
    view = (await test_client.get(f"/v1/projects/{project_id}/budget")).json()
    assert Decimal(str(view["spent_usd"])) == Decimal(".1")
    assert Decimal(str(view["reserved_usd"])) == Decimal(".9")
    assert Decimal(str(view["available_usd"])) == 0
    assert view["accounting_status"] == "pending"
    ledger = await db_session.scalar(select(BudgetUsage).where(BudgetUsage.entity_type == "project", BudgetUsage.period_type == "run"))
    assert ledger.period_start == PROJECT_EPOCH and ledger.request_count == 1 and ledger.total_cost_usd == Decimal(".1")


async def test_prior_uncapped_usage_counts_when_budget_added(test_client, db_session, source_member, product, meter):
    scope = await resolve_project(db_session, tenant_id=source_member.org_id, product_id=product["product_id"])
    target = await prepare_project_budget(db_session, meter, scope)
    await db_session.commit()
    assert (await meter.reserve("before-cap", Decimal("2"), [target])).admitted
    await settle_usage(
        db_session,
        org_id=source_member.org_id,
        request_id="before-cap",
        user_id=source_member.user_id,
        cost=Decimal("1.5"),
        total_tokens=5,
        entities=[("project", scope.project_id)],
        reservation_scope_keys=[target.key()],
    )
    await db_session.commit()
    result = await test_client.put(f"/v1/projects/{scope.project_id}/budget", json={"budget_usd": "1.00"})
    assert result.status_code == 200 and Decimal(str(result.json()["available_usd"])) == 0
    target = await prepare_project_budget(db_session, meter, scope)
    assert not (await meter.reserve("over-cap", Decimal(".01"), [target])).admitted
    await meter._client.delete(target.key())
    with pytest.raises(ValueError, match="cannot be confirmed"):
        await prepare_project_budget(db_session, meter, scope)
    assert not await meter._client.exists(target.key())


async def test_bounded_unknown_cost_survives_time_and_does_not_block_unused_headroom(db_session, product, source_member, meter):
    scope = await resolve_project(db_session, tenant_id=source_member.org_id, product_id=product["product_id"])
    target = replace(await prepare_project_budget(db_session, meter, scope), headroom_usd=Decimal("1"), pending_marker_seconds=1000000000000)
    await db_session.commit()
    clock = meter._clock()
    assert (await meter.reserve("lost", Decimal(".5"), [target])).admitted
    meter._clock = lambda: clock + 1000000
    assert (await meter.snapshot(target)).total_usd == Decimal(".5")
    assert (await meter.reserve("new", Decimal(".2"), [target])).admitted
    assert not (await meter.reserve("too-much", Decimal(".4"), [target])).admitted


async def test_members_can_read_but_only_owner_can_set_budget(test_client, app, db_session, source_member, product, meter):
    url = f"/v1/projects/{product['project_id']}/budget"
    teammate = source_member.model_copy(update={"user_id": "teammate"})
    db_session.add(User(id="teammate", org_id=source_member.org_id, team_id=source_member.team_id, email="team@example.test", user_kind="human"))
    await db_session.flush()
    member = TeamMembership(user_id="teammate", org_id=source_member.org_id, team_id=source_member.team_id, is_primary=True)
    db_session.add(member)
    await db_session.commit()
    app.dependency_overrides[get_current_user] = lambda: teammate
    assert (await test_client.get(url)).status_code == 200
    assert (await test_client.put(url, json={"budget_usd": "10"})).status_code == 403
    await db_session.delete(member)
    await db_session.commit()
    assert (await test_client.get(url)).status_code == 404
    with pytest.raises(ProjectScopeError):
        await authorize_project_selection(db_session, user_id="teammate", tenant_id=source_member.org_id, product_id=product["product_id"])


async def test_disabled_enforcement_observation_is_not_forgotten(db_session, product, source_member, meter):
    scope = await resolve_project(db_session, tenant_id=source_member.org_id, product_id=product["product_id"])
    target = await prepare_project_budget(db_session, meter, scope, enforce=False)
    await db_session.commit()
    assert await meter.observe("off-mode", [target])
    assert not (await meter.reserve("after-enable", Decimal(".1"), [target])).admitted
    await settle_usage(
        db_session,
        org_id=source_member.org_id,
        request_id="off-mode",
        user_id=source_member.user_id,
        cost=Decimal(".25"),
        total_tokens=3,
        entities=[("project", scope.project_id)],
        reservation_scope_keys=[target.key()],
    )
    await db_session.commit()
    await reconcile_project_budget(db_session, meter, target)
    assert (await meter.snapshot(target)).total_usd == Decimal(".25")
    assert (await meter.reserve("after-receipt", Decimal(".1"), [target])).admitted


async def test_legacy_disabled_checks_cannot_silently_bypass_project_accounting(db_session, product, source_member, monkeypatch):
    from src.budget import enforcement_service

    context = source_member.model_copy()
    context._project_attribution = await resolve_project(db_session, tenant_id=source_member.org_id, product_id=product["product_id"])
    monkeypatch.setattr(enforcement_service.budget_config, "budget_check_enabled", False)
    outcome = await budget_enforcement_service.check_budget_hierarchy(context, Decimal(".1"), request_id="paid")
    assert not outcome.allowed and outcome.scope == "project"
