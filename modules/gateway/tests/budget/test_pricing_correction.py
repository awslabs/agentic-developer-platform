"""Incident credits preserve original replay identity and every budget rung."""

import asyncio
import copy
import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from pricing_policy import RoutingEvidence, build_pricing_decision, load_snapshot, normalize_usage
from src.budget.pricing_correction import allocation_from_log, correct_request
from src.budget.pricing_decisions import _bundled_estimate_row
from src.budget.settlement import settle_usage
from src.shared.models.budget import BudgetPricingCorrection, BudgetSettlementReceipt, BudgetUsage
from src.shared.models.usage import UsageLog
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401


@pytest.fixture
def log():
    snapshot = load_snapshot("2026-09-24.2")
    evidence = RoutingEvidence(
        original_model_id="openai.gpt-6-sol",
        billing_model_id="openai.gpt-6-sol",
        forwarded_model_id="us.openai.gpt-6-sol",
        endpoint_region="us-east-1",
        geography="geo_cris",
        served_service_tier_raw="default",
    )
    decision = build_pricing_decision(
        request_id="request",
        org_id="tenant",
        usage=normalize_usage(
            {"input_tokens": 201635, "output_tokens": 1169, "input_tokens_details": {"cached_tokens": 201153, "cache_write_tokens": 480}},
            api_format="openai",
        ),
        evidence=RoutingEvidence(
            original_model_id="openai.gpt-6-sol",
            billing_model_id="openai.gpt-6-sol",
            forwarded_model_id="us.openai.gpt-6-sol",
            endpoint_region="us-east-1",
            geography="geo_cris",
            served_service_tier_raw="default",
        ),
        rows=(_bundled_estimate_row("openai.gpt-6-sol", evidence, snapshot),),
        extra_reasons=("unknown_model",),
        snapshot=snapshot,
    )
    return {
        "request_id": "request",
        "org_id": "tenant",
        "user_id": "worker",
        "account_type": "service",
        "root_human_id": "human",
        "team_id": "team",
        "department_id": "dept",
        "timestamp": "2026-09-28T00:16:04Z",
        "pricing_decision": decision.to_dict(),
    }


@pytest.fixture
async def ledger(pg_url, log):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as conn:
        for model in (BudgetSettlementReceipt, BudgetUsage, BudgetPricingCorrection, UsageLog):
            await conn.run_sync(lambda sync, model=model: model.__table__.create(sync))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db, db.begin():
        _, entities, _ = allocation_from_log(log)
        await settle_usage(
            db,
            org_id="tenant",
            request_id="request",
            user_id="worker",
            cost="0.622440",
            total_tokens=202804,
            entities=entities,
            timestamp=datetime(2026, 9, 28, tzinfo=UTC),
        )
        db.add(
            UsageLog(
                id="log",
                org_id="tenant",
                user_id="worker",
                account_type="service",
                team_id="team",
                department_id="dept",
                model="openai.gpt-6-sol",
                input_tokens=2,
                output_tokens=1169,
                cost_usd=Decimal("0.622440"),
                latency_ms=1,
                status_code=200,
                request_id="request",
                pricing_decision=log["pricing_decision"],
            )
        )
    yield factory
    await engine.dispose()


async def correct(factory, log, apply=True):
    async with factory() as db, db.begin():
        return await correct_request(
            db, org_id="tenant", request_id="request", log=log, source_key="tenant/worker/receipt.json", actor="incident-test", apply=apply
        )


@pytest.mark.asyncio
async def test_credit_updates_every_rung_once_and_keeps_original_replay(ledger, log):
    preview = await correct(ledger, log, apply=False)
    assert preview["credit_usd"] == "0.564003"
    results = await asyncio.gather(*(correct(ledger, log) for _ in range(4)))
    assert [r["status"] for r in results].count("corrected") == 1
    async with ledger() as db, db.begin():
        rows = (await db.scalars(select(BudgetUsage))).all()
        assert len(rows) == 18
        assert all(r.total_cost_usd == Decimal("0.058437") and r.request_count == 1 and r.total_tokens == 202804 for r in rows)
        receipt = await db.get(BudgetSettlementReceipt, ("tenant", "request"))
        assert receipt.cost_usd == Decimal("0.622440")
        usage = await db.get(UsageLog, "log")
        assert usage.cost_usd == Decimal("0.058437")
        audit = (await db.scalars(select(BudgetPricingCorrection))).one()
        assert audit.original_decision == log["pricing_decision"]
        assert audit.corrected_decision == usage.pricing_decision
        _, entities, _ = allocation_from_log(log)
        assert not await settle_usage(
            db,
            org_id="tenant",
            request_id="request",
            user_id="worker",
            cost="0.622440",
            total_tokens=202804,
            entities=entities,
            timestamp=datetime(2026, 9, 28, tzinfo=UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("org_id", "other"), ("root_human_id", "wrong"), ("user_id", "other"), ("timestamp", "2026-09-27T00:00:00Z")]
)
async def test_wrong_tenant_owner_or_allocation_cannot_credit(ledger, log, field, value):
    bad = copy.deepcopy(log)
    bad[field] = value
    with pytest.raises(ValueError):
        await correct(ledger, bad)
    async with ledger() as db:
        assert not (await db.scalars(select(BudgetPricingCorrection))).all()
        assert all(r.total_cost_usd == Decimal("0.622440") for r in (await db.scalars(select(BudgetUsage))).all())


@pytest.mark.asyncio
async def test_correction_rolls_back_atomically(ledger, log):
    with pytest.raises(RuntimeError):
        async with ledger() as db, db.begin():
            await correct_request(db, org_id="tenant", request_id="request", log=log, source_key="receipt", actor="test", apply=True)
            raise RuntimeError("interrupted")
    assert (await correct(ledger, log))["status"] == "corrected"


@pytest.mark.asyncio
async def test_operator_plan_apply_and_changed_plan_rollback(ledger, log, monkeypatch, capsys):
    from src.budget import reconcile_gpt6 as repair

    client = Mock()
    client.get_caller_identity.return_value = {"Account": "123456789012", "Arn": "maintenance-role"}
    client.get_object.side_effect = lambda **kwargs: {"Body": io.BytesIO(json.dumps(log).encode())}
    monkeypatch.setattr(repair.boto3, "client", lambda name: client)
    monkeypatch.setattr(repair, "get_session_factory", lambda: ledger)
    args = SimpleNamespace(
        account_id="123456789012",
        org_id="tenant",
        bucket="trusted-logs",
        actor="operator",
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2030, 1, 1, tzinfo=UTC),
        apply=False,
        plan_sha256=None,
    )
    await repair.reconcile(args)
    plan = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert plan["credit_usd"] == "0.564003" and not plan["skipped"]
    args.apply = True
    args.plan_sha256 = "wrong"
    with pytest.raises(ValueError, match="plan changed"):
        await repair.reconcile(args)
    async with ledger() as db:
        assert not (await db.scalars(select(BudgetPricingCorrection))).all()
    args.plan_sha256 = plan["plan_sha256"]
    await repair.reconcile(args)
    applied = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert applied["applied"] and applied["credit_usd"] == plan["credit_usd"]
    client.get_object.assert_called_with(Bucket="trusted-logs", Key=client.get_object.call_args.kwargs["Key"], ExpectedBucketOwner="123456789012")
