"""Real SQL recovery of a successful model call after gateway accounting failure."""

# ruff: noqa: F401, F811
import asyncio
import importlib
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from pricing_policy import RoutingEvidence, build_pricing_decision, load_snapshot, normalize_usage
from src.shared.models.budget import BudgetSettlementReceipt, BudgetUsage
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage.persistence import persist_usage_with_retry
from src.usage.service import UsageService
from tests.budget.test_settlement_receipts import ledger, pg_server, pg_url
from tests.usage.test_persistence import database_error


def event():
    usage = {"input_tokens": 206596, "output_tokens": 555, "cache_read_input_tokens": 205627, "cache_creation_input_tokens": 967}
    snapshot = load_snapshot()
    decision = build_pricing_decision(
        request_id="recovered",
        org_id="tenant",
        usage=normalize_usage(usage, api_format="openai"),
        evidence=RoutingEvidence(
            original_model_id="openai.gpt-6-astra",
            billing_model_id="openai.gpt-6-astra",
            served_service_tier_raw="standard",
            geography="in_region",
            endpoint_region="us-east-1",
        ),
        rows=snapshot.rates,
        snapshot=snapshot,
    )
    return {
        "settlement_version": 1,
        "request_id": "recovered",
        "org_id": "tenant",
        "user_id": "worker",
        "timestamp": datetime.now(UTC).isoformat(),
        "team_id": "team",
        "department_id": "dept",
        "root_human_id": "human",
        "account_type": "service",
        "model": "openai.gpt-6-astra",
        "api_format": "openai",
        "latency_ms": 19000,
        "request": {},
        "response": {"usage": usage},
        "pricing_decision": decision.to_dict(),
        "usage_recovery": {
            "version": 1,
            "agent_run_id": "reviewer",
            "graph_address": "flow/epic/wave/review",
            "status_code": 200,
            "reservation_scope_keys": [
                "budget:resv:{tenant}:flow:flow:run:lifetime",
                "budget:resv:{tenant}:run:reviewer:run:lifetime",
                "budget:resv:{tenant}:chain:chain:run:lifetime",
            ],
        },
    }, decision


@pytest.mark.parametrize("invalid", ["foreign_scope", "invalid_usage", "repriced", "invalid_run", "project_tenant", "project_run"])
def test_tracker_rejects_invalid_recovery_evidence_before_writing(invalid):
    tracker = importlib.import_module("tests.lambda._handler_loader").load_handler("budget-usage-tracker")
    log, decision = event()
    evidence = decision.to_dict()
    if invalid == "foreign_scope":
        log["usage_recovery"]["reservation_scope_keys"] = ["budget:resv:{other}:run:reviewer:run:lifetime"]
    if invalid == "invalid_usage":
        evidence["usage"]["valid"] = False
    if invalid == "invalid_run":
        log["usage_recovery"]["agent_run_id"] = {"unexpected": "shape"}
    if invalid.startswith("project_"):
        log["usage_recovery"]["project_attribution"] = {
            "tenant_id": "other" if invalid == "project_tenant" else "tenant",
            "product_id": "product",
            "project_id": "base",
            "run_id": "other" if invalid == "project_run" else "reviewer",
        }
    conn = MagicMock()
    with pytest.raises(ValueError):
        tracker.recover_gateway_usage(conn, log, SimpleNamespace(reused=invalid != "repriced", decision=evidence), "usage.json")
    conn.cursor.assert_not_called()


@pytest.mark.parametrize("already_settled,project_scoped", [(False, False), (True, False), (False, True)])
async def test_tracker_recovers_missing_usage_and_scope_links_without_duplicate_debits(ledger, pg_url, already_settled, project_scoped):
    import psycopg2

    from pricing_policy.storage import V2RateCache

    tracker = importlib.import_module("tests.lambda._handler_loader").load_handler("budget-usage-tracker")
    source = V2RateCache().state(monotonic=0.0, now_iso="2026-09-12T00:00:00+00:00")
    log, decision = event()
    if project_scoped:
        log["usage_recovery"]["project_attribution"] = {
            "tenant_id": "tenant",
            "product_id": "product",
            "project_id": "base",
            "flow_id": "flow",
            "run_id": "reviewer",
        }
        log["usage_recovery"]["reservation_scope_keys"].append("budget:resv:{tenant}:project:base:run:1970-01-01")
    async with ledger() as db:
        await db.run_sync(lambda sync: UsageLog.__table__.create(sync.connection()))
        await db.commit()

    def deliver(with_recovery=True):
        payload = dict(log)
        if not with_recovery:
            payload.pop("usage_recovery")
        with psycopg2.connect(pg_url) as conn:
            tracker.process_chat_log(conn, payload, source, chat_log_s3_key="usage.json")

    if already_settled:
        await asyncio.to_thread(deliver, False)  # Reproduce the missing-scope legacy receipt.
    await asyncio.to_thread(deliver)
    await asyncio.to_thread(deliver)
    async with ledger() as db:
        usage = (await db.scalars(select(UsageLog))).one()
        assert usage.agent_run_id == "reviewer" and usage.cost_usd == decision.ledger_cost
        assert usage.cache_read_input_tokens == 205627 and usage.cache_creation_input_tokens == 967
        assert usage.pricing_decision == decision.to_dict()
        receipt = await db.get(BudgetSettlementReceipt, ("tenant", "recovered"))
        assert set(receipt.reservation_scope_keys) == set(log["usage_recovery"]["reservation_scope_keys"])
        budgets = (await db.scalars(select(BudgetUsage))).all()
        assert budgets and all(row.request_count == 1 and row.total_cost_usd == decision.ledger_cost for row in budgets)
        if project_scoped:
            assert (usage.product_id, usage.project_id, usage.flow_id) == ("product", "base", "flow")
            lifetime = [row for row in budgets if row.entity_type == "project" and row.period_type == "run"]
            assert len(lifetime) == 1 and str(lifetime[0].period_start) == "1970-01-01"


async def test_retry_after_lost_commit_acknowledgement_records_one_charge(ledger):
    log, decision = event()
    context = TokenContext(
        org_id="tenant", user_id="worker", team_id="team", department_id="dept", account_type="human", expires_at=datetime.now(UTC)
    )
    async with ledger() as db:
        await db.run_sync(lambda sync: UsageLog.__table__.create(sync.connection()))
        await db.commit()
    attempts = 0

    async def write():
        nonlocal attempts
        attempts += 1
        async with ledger() as db:
            await UsageService(db).log_request(
                context=context,
                model=log["model"],
                input_tokens=2,
                output_tokens=555,
                cost_usd=decision.ledger_cost,
                latency_ms=1,
                status_code=200,
                request_id="recovered",
                pricing_decision=decision,
            )
        if attempts == 1:
            raise database_error("08006")

    await persist_usage_with_retry(write, request_id="recovered", retry_safe=True)
    assert attempts == 2
    async with ledger() as db:
        assert len((await db.scalars(select(UsageLog))).all()) == 1
        assert all(row.request_count == 1 for row in (await db.scalars(select(BudgetUsage))).all())
