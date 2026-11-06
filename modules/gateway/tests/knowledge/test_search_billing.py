"""Paid search denies before dispatch and settles only provider-reported usage."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis.aioredis
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from pricing_policy import RateRow, verify_pricing_decision
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationStore, ReservationTarget
from src.knowledge import search_billing as billing
from src.shared.models.budget import BudgetSettlementReceipt, BudgetUsage
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import DenyReason, EnforcementResult
from tests.activity.test_tool_export import export_service  # noqa: F401
from tests.budget.test_settlement_receipts import ledger, pg_server, pg_url  # noqa: F401


def context():
    return TokenContext(
        user_id="owner",
        org_id="tenant",
        team_id="team",
        department_id="department",
        account_type="human",
        auth_source="jwt",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def receipt(tokens=1000, **changes):
    return {"version": 1, "model": billing.MODEL, "status": "measured", "input_tokens": tokens, **changes}


@pytest.fixture
def admission(monkeypatch):
    budget = Mock(check_budget_hierarchy=AsyncMock(return_value=EnforcementResult(allowed=True)), reconcile_reservation=AsyncMock())
    activity = Mock()
    rate = SimpleNamespace(allowed=True)
    rate_service = Mock(consume_tool_rate_limit=AsyncMock(return_value=rate))

    @asynccontextmanager
    async def database():
        yield Mock()

    monkeypatch.setattr(billing, "get_accounting_session_factory", lambda: database)
    monkeypatch.setattr(billing, "lock_legacy_metering_gates", AsyncMock())
    monkeypatch.setattr(billing, "BudgetEnforcementService", lambda: budget)
    monkeypatch.setattr(billing, "ActivityService", lambda **kwargs: activity)
    monkeypatch.setattr(billing, "get_rate_limit_service", lambda request: rate_service)
    monkeypatch.setattr(billing, "require_ordinary_tool_identity", AsyncMock())
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", "search-test")
    return SimpleNamespace(budget=budget, activity=activity, rate=rate_service)


@pytest.mark.parametrize("denial,status", [(DenyReason.BUDGET_EXCEEDED, 429), (DenyReason.CHECK_UNAVAILABLE, 503)])
async def test_budget_refusal_never_records_an_allowed_operation(admission, denial, status):
    admission.budget.check_budget_hierarchy.return_value = EnforcementResult(allowed=False, deny_reason=denial)
    with pytest.raises(HTTPException) as error:
        await billing.admit_search(Mock(), context(), subject="login", human_id="owner")
    assert error.value.status_code == status
    admission.activity.record_tool_call.assert_not_called()
    call = admission.budget.check_budget_hierarchy.await_args
    assert call.kwargs["tool_estimate"] is True
    assert call.args[1] == Decimal("0.000164")


async def test_rate_and_native_refusals_precede_budget(admission, monkeypatch):
    admission.rate.consume_tool_rate_limit.return_value = SimpleNamespace(allowed=False, retry_after_seconds=7)
    with pytest.raises(HTTPException) as error:
        await billing.admit_search(Mock(), context(), subject="login", human_id="owner")
    assert error.value.status_code == 429
    assert error.value.headers["Retry-After"] == "7"
    admission.budget.check_budget_hierarchy.assert_not_awaited()
    monkeypatch.setattr(billing, "require_ordinary_tool_identity", AsyncMock(side_effect=HTTPException(403, "native designation")))
    with pytest.raises(HTTPException) as error:
        await billing.admit_search(Mock(), context(), subject="login", human_id="owner")
    assert error.value.status_code == 403
    admission.budget.check_budget_hierarchy.assert_not_awaited()


async def test_failed_activity_write_releases_only_the_unstarted_call(admission):
    admission.activity.record_tool_call.side_effect = RuntimeError("store unavailable")
    with pytest.raises(RuntimeError):
        await billing.admit_search(Mock(), context(), subject="login", human_id="owner")
    call = admission.budget.reconcile_reservation.await_args
    assert call.kwargs["actual_cost_usd"] == 0
    assert call.kwargs["usage_known"] is True


async def test_direct_billing_subject_stays_separate_from_canonical_activity_identity(admission):
    owner = context().model_copy(update={"user_id": "login-owner"})
    admitted = await billing.admit_search(Mock(), owner, subject="login-owner", human_id="owner")
    assert admission.budget.check_budget_hierarchy.await_args.args[0].user_id == "login-owner"
    assert admission.activity.record_tool_call.call_args.kwargs["user_id"] == "owner"
    assert billing.require_ordinary_tool_identity.await_args.kwargs["human_id"] == "owner"
    assert set(billing.require_ordinary_tool_identity.await_args.kwargs["aliases"]) == {"owner", "login-owner"}
    assert admitted._activity_identity()["user_id"] == "owner"


@pytest.mark.parametrize(
    "value", [None, {}, receipt(status="unknown"), receipt(model="other"), receipt(tokens=True), receipt(tokens=8193), receipt(tokens=0)]
)
async def test_unknown_usage_never_releases_or_debits_estimated_spend(admission, value):
    admitted = await billing.admit_search(Mock(), context(), subject="login", human_id="owner")
    admitted.dispatched = True
    admitted.result = {"embedding_usage": value}
    assert not await billing.finalize_search(admitted)
    assert admission.budget.reconcile_reservation.await_args.kwargs == {"usage_known": False}
    assert admission.activity.record_tool_accounting.call_args.kwargs["status"] == "pending"


@pytest.mark.parametrize("failed_search", [False, True])
async def test_provider_usage_is_settled_even_when_search_later_fails(ledger, admission, monkeypatch, failed_search):  # noqa: F811
    async with ledger() as db:
        await db.run_sync(lambda sync: UsageLog.__table__.create(sync.connection()))
        await db.commit()
    monkeypatch.setattr(billing, "get_accounting_session_factory", lambda: ledger)
    owner = context().model_copy(update={"user_id": "login-owner"})
    admitted = await billing.admit_search(Mock(), owner, subject="login-owner", human_id="owner")
    admitted.dispatched = True
    admitted.result = {"embedding_usage": receipt(), "results": [], **({"error": "vector unavailable"} if failed_search else {})}
    # Two finalizations, one server request: one debit and one usage row.
    assert await billing.finalize_search(admitted)
    assert await billing.finalize_search(admitted)
    async with ledger() as db:
        rows = (await db.scalars(select(UsageLog))).all()
        assert len(rows) == 1 and rows[0].input_tokens == 1000 and rows[0].user_id == "login-owner"
        assert rows[0].cost_usd == Decimal("0.000020")
        assert rows[0].pricing_confidence == "estimated"
        assert rows[0].status_code == (503 if failed_search else 200)
        budgets = (await db.scalars(select(BudgetUsage))).all()
        assert len(budgets) == 12
        assert {row.entity_id for row in budgets if row.entity_type == "user"} == {"login-owner"}
        assert all(row.request_count == 1 and row.total_cost_usd == Decimal("0.000020") for row in budgets)
        assert len((await db.scalars(select(BudgetSettlementReceipt))).all()) == 1
    assert admission.activity.record_tool_accounting.call_args.kwargs["status"] == "settled"


async def test_failed_settlement_keeps_original_hold(admission, monkeypatch):
    admitted = await billing.admit_search(Mock(), context(), subject="login", human_id="owner")
    admitted.dispatched = True
    admitted.result = {"embedding_usage": receipt()}
    monkeypatch.setattr(billing, "persist_usage_with_retry", AsyncMock(side_effect=RuntimeError("write failed")))
    assert not await billing.finalize_search(admitted)
    admission.budget.reconcile_reservation.assert_not_awaited()
    assert admission.activity.record_tool_accounting.call_args.kwargs["status"] == "pending"


async def test_client_cancellation_still_finishes_accounting(admission, monkeypatch):
    admitted = await billing.admit_search(Mock(), context(), subject="login", human_id="owner")
    started, finish = asyncio.Event(), asyncio.Event()

    async def finalizer():
        started.set()
        await finish.wait()
        return True

    monkeypatch.setattr(admitted, "finish", AsyncMock(side_effect=finalizer))
    task = asyncio.create_task(billing.finalize_search(admitted))
    await started.wait()
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert admitted.finish.await_count == 1


def test_known_model_pricing_is_pinned_and_unknown_or_zero_pricing_is_refused(monkeypatch):
    price = billing.SearchPrice.load()
    decision = price.decision(context(), "search-one", 1000)
    assert verify_pricing_decision(decision.to_dict(), request_id="search-one", org_id="tenant") == Decimal("0.000020")
    assert decision.source_kind == "bundled_snapshot"
    assert decision.confidence == "estimated"
    for known, rate in ((False, "0.00002"), (True, "0"), (True, "NaN")):
        monkeypatch.setattr(billing, "resolve_curated_non_openai", lambda *args, **kwargs: ({"input": Decimal(rate), "output": Decimal("0")}, known))
        with pytest.raises(ValueError):
            billing.SearchPrice.load()


def test_zero_output_rate_does_not_allow_free_text_generation():
    price = billing.SearchPrice.load()
    row = vars(price.row)
    with pytest.raises(ValueError):
        RateRow.from_mapping({**row, "model_id": "unknown-text-model"})
    with pytest.raises(ValueError):
        from pricing_policy import normalize_usage, price_from_rate_row

        price_from_rate_row(price.row, normalize_usage({"input_tokens": 100, "output_tokens": 1}, api_format="openai"))


async def test_concurrent_searches_contend_on_one_atomic_cap_and_uncertain_holds_expire():
    price = billing.SearchPrice.load()
    clock = [1000.0]
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(redis_url=None, ttl_seconds=120, clock=lambda: clock[0])
    store._client = redis
    store._register_scripts(redis)
    service = BudgetEnforcementService(reservations=store)
    target = ReservationTarget("tenant", "user", "owner", "daily", "2026-10-27", price.ceiling * 3, ttl_seconds=600)
    # Exercise the same strict atomic gate called by tool_estimate admission.
    outcomes = await asyncio.gather(*(service._reserve_or_degrade(str(i), price.ceiling, [target], strict=True) for i in range(12)))
    assert outcomes.count(None) == 3
    assert all(outcome.deny_reason == DenyReason.BUDGET_EXCEEDED for outcome in outcomes if outcome is not None)
    clock[0] += 121
    assert await service._reserve_or_degrade("later", price.ceiling, [target], strict=True) is not None
    clock[0] += 480
    assert await service._reserve_or_degrade("new-period", price.ceiling, [target], strict=True) is None
    await redis.aclose()


async def test_confirmed_settlement_releases_full_period_hold_after_normal_overlap(monkeypatch):
    clock = [1000.0]
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = ReservationStore(redis_url=None, ttl_seconds=120, clock=lambda: clock[0])
    store._client = redis
    store._register_scripts(redis)
    target = ReservationTarget("tenant", "user", "owner", "daily", "2026-10-27", Decimal("1"), ttl_seconds=600)
    owner = context()
    owner._budget_admission_targets = [target]
    admitted = billing.SearchAdmission(owner, billing.SearchPrice.load(), BudgetEnforcementService(reservations=store), Mock(), "owner")
    await store.reserve(admitted.request_id, admitted.price.ceiling, [target])
    admitted.dispatched = True
    admitted.result = {"embedding_usage": receipt(), "results": []}
    monkeypatch.setattr(billing, "persist_usage_with_retry", AsyncMock())
    assert await admitted.finish()
    amounts = await store.live_request_amounts(target)
    assert amounts[admitted.request_id] == Decimal("0.000020")
    clock[0] += 121
    assert not await store.live_request_amounts(target)
    await redis.aclose()


def test_tool_activity_preserves_pending_then_records_owned_settlement(export_service):  # noqa: F811
    identity = {"receipt_id": "search-one", "user_id": "owner", "tenant_id": "tenant", "tool": billing.TOOL}
    price = billing.SearchPrice.load()
    export_service.record_tool_call(**identity, estimated_cost_usd=price.ceiling, run_id=None)
    export_service.record_tool_accounting(**identity, status="pending")
    item = export_service._table.scan()["Items"][0]
    assert item["settled_cost_usd"] is None and item["accounting_status"] == "pending"
    decision = price.decision(context(), "search-one", 1000).to_dict()
    with pytest.raises(ValueError):
        export_service.record_tool_accounting(**{**identity, "tenant_id": "other"}, status="settled", decision=decision)
    export_service.record_tool_accounting(**identity, status="settled", decision=decision)
    export_service.record_tool_accounting(**identity, status="pending")
    item = export_service._table.scan()["Items"][0]
    assert item["settled_cost_usd"] == Decimal("0.000020")
    assert item["observed_usage_status"] == "provider_reported"
    assert item["accounting_status"] == "settled"
