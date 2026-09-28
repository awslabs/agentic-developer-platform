# ruff: noqa: F811
"""Scoped, audited controls with optimistic concurrency and conservative defaults."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.admin.exceptions import AccessDeniedError
from src.budget import enforcement_routes as routes
from src.budget.enforcement_settings import BudgetAccountingGap, BudgetEnforcementSetting, flow_key, read_enforcement
from src.shared.models.audit import AuditLog
from tests.orchestration.test_policy_admission import _make_flow, _make_org, engine, session  # noqa: F401


@pytest.fixture
async def controls(session, monkeypatch):
    await _make_org(session)
    flow = await _make_flow(session)
    await session.commit()
    monkeypatch.setattr(routes, "AccessControl", lambda db: SimpleNamespace(require_platform_admin=lambda user: None, check_permission=AsyncMock()))
    monkeypatch.setattr(routes, "authorize_human_session", AsyncMock(return_value=SimpleNamespace(user_id="operator")))
    return SimpleNamespace(db=session, flow=flow, user=SimpleNamespace(org_id=flow.org_id))


def body(enabled, revision=0):
    return routes.EnforcementUpdate(enabled=enabled, expected_revision=revision, reason="Operator budget control")


async def test_default_on_and_explicit_global_overrides_environment(session, monkeypatch):
    monkeypatch.setenv("BUDGET_ENFORCEMENT_ENABLED", "true")
    assert (await read_enforcement(session)).enabled
    monkeypatch.setenv("BUDGET_ENFORCEMENT_ENABLED", "false")
    assert not (await read_enforcement(session)).enabled
    session.add(BudgetEnforcementSetting(scope_key="global", enabled=True, revision=1, updated_by="operator"))
    await session.flush()
    assert (await read_enforcement(session)).enabled


async def test_global_and_flow_switches_are_audited_and_reversible(controls):
    c = controls
    first = await routes.change(c.db, c.user, body(False), c.flow.id)
    assert not first["effective_enabled"] and first["global_enabled"] and first["revision"] == 1
    assert (await routes.status(c.db, c.user))["effective_enabled"]
    assert (await routes.change(c.db, c.user, body(True, 1), c.flow.id))["effective_enabled"]
    assert not (await routes.change(c.db, c.user, body(False)))["effective_enabled"]
    flow = await routes.status(c.db, c.user, c.flow.id)
    assert flow["flow_enabled"] and not flow["effective_enabled"]
    audits = list(await c.db.scalars(select(AuditLog).where(AuditLog.event_type == "budget_enforcement_changed")))
    assert len(audits) == 3 and all(a.actor_id == "operator" for a in audits)


@pytest.mark.parametrize("scope", ["global", "flow"])
async def test_stale_write_is_rejected_without_extra_audit(controls, scope):
    c = controls
    flow = c.flow.id if scope == "flow" else None
    await routes.change(c.db, c.user, body(False), flow)
    with pytest.raises(HTTPException) as exc:
        await routes.change(c.db, c.user, body(True), flow)
    assert exc.value.status_code == 409
    assert not (await routes.status(c.db, c.user, flow))["effective_enabled"]
    assert len(list(await c.db.scalars(select(AuditLog)))) == 1


async def test_cross_tenant_flow_cannot_be_read_or_changed(controls):
    c = controls
    c.user.org_id = "another-tenant"
    with pytest.raises(HTTPException) as exc:
        await routes.get_flow(c.flow.id, c.user, c.db)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await routes.change(c.db, c.user, body(False), c.flow.id)
    assert exc.value.status_code == 404
    assert not list(await c.db.scalars(select(BudgetEnforcementSetting)))


async def test_toggle_never_deletes_accounting_gap(controls):
    c = controls
    c.db.add(BudgetAccountingGap(request_id="missing-observation", scope_key=flow_key(c.flow.org_id, c.flow.id)))
    await c.db.commit()
    assert (await routes.change(c.db, c.user, body(False), c.flow.id))["accounting_incomplete"]
    assert (await routes.change(c.db, c.user, body(True, 1), c.flow.id))["accounting_incomplete"]


async def test_nonadmin_cannot_change_either_scope():
    for flow in (None, "flow"):
        with pytest.raises(AccessDeniedError):
            await routes.change(None, SimpleNamespace(is_admin=False), body(False), flow)


async def test_worker_cannot_use_admin_flag_to_change_control(monkeypatch):
    monkeypatch.setattr(routes, "AccessControl", lambda db: SimpleNamespace(require_platform_admin=lambda user: None, check_permission=AsyncMock()))
    user = SimpleNamespace(is_admin=True, account_type="service", auth_source="iam", org_id="org")
    with pytest.raises(HTTPException) as exc:
        await routes.change(None, user, body(False))
    assert exc.value.status_code == 403


async def test_global_off_keeps_legacy_nonpolicy_usage_reconciliation(session, monkeypatch):
    """Non-Claude usage keeps the existing token-pricing path, without a false gap."""
    from decimal import Decimal

    import fakeredis.aioredis

    from src.budget.enforcement_service import BudgetEnforcementService
    from src.budget.reservations import ReservationStore, ReservationTarget
    from tests.budget.test_budget_overshoot import _context

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(redis_url=None, ttl_seconds=86400, client=client)
    service = BudgetEnforcementService(db_session=session)
    service._reservations = store
    token = _context()
    token._budget_enforcement_enabled = False
    target = ReservationTarget(
        org_id=token.org_id, entity_type="run", entity_id="legacy", period_type="run", period_start="lifetime", headroom_usd=Decimal(0)
    )
    token._run_scope_reservations = [target]
    assert await store.observe("legacy-off-call", [target])
    monkeypatch.setattr(service._pricing, "calculate_cost", lambda model, input_tokens, output_tokens: Decimal("0.123456"))
    await service.reconcile_reservation(token, "legacy-off-call", "amazon.nova-pro-v1:0", 1000, 100)
    fields = await client.hgetall(target.key())
    assert Decimal(fields["legacy-off-call"].split(":")[0]) == Decimal("0.123456")
    assert "unbounded:legacy-off-call" not in fields
    assert not list(await session.scalars(select(BudgetAccountingGap)))
    await client.aclose()
