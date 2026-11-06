"""SQL/S3 independent oracles for receipt-backed exactly-once settlement."""

import asyncio
import json
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import psycopg2
import pytest

from pricing_policy import RoutingEvidence
from src.budget.metering_admission import MeteringBudgetExceededError
from src.orchestration.provider_quotes import TrustedUsage
from tests.budget import test_metering_scope_admission as scope_fixtures
from tests.budget.test_metering_scope_admission import BODY, admission_for, context, insert_budget, prepare
from tests.proxy.test_mantle_metering_admission import MODEL, USAGE, quote, service_for

pytest_plugins = ("tests.budget.test_metering_scope_admission",)
DESTINATION = "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses"


# A collected test module is not a reliable source of global plugin fixtures.
# Give these fixtures local scope, including when CI selects only this file.
@pytest.fixture
def metering_db(pg_head_url):
    return scope_fixtures.metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def journal():
    yield from scope_fixtures.journal.__wrapped__()


async def complete(admission, caller, request_id, *, usage=None):
    attempt = await prepare(admission, caller, request_id)
    await admission.arm(attempt, submitted=BODY, model=MODEL, destination=DESTINATION)
    measured = usage or USAGE
    routing = RoutingEvidence(
        original_model_id=MODEL,
        billing_model_id=MODEL,
        forwarded_model_id=MODEL,
        endpoint_host="bedrock-mantle.us-east-1.api.aws",
        endpoint_region="us-east-1",
        geography="in_region",
        served_service_tier_raw="standard",
    )
    decision = admission.price_final(
        pricing_basis=attempt.pricing_basis,
        usage=measured,
        routing=routing,
        request_id=request_id,
        tenant_id=caller.org_id,
        bound=attempt.quote.maximum_cost_usd,
    )
    receipt = await admission.write_final(
        attempt=attempt,
        usage=measured,
        trusted=TrustedUsage(measured["input_tokens"], measured["output_tokens"], True),
        provider_request_id="controlled-provider",
        decision=decision,
        latency_ms=12,
    )
    return attempt, receipt, decision


def ledger(pg_url, request_id):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT state, final_version FROM metering_attempts WHERE request_id=%s", (request_id,))
        attempt = cursor.fetchone()
        cursor.execute("SELECT cost_usd, total_tokens FROM budget_settlement_receipts WHERE request_id=%s", (request_id,))
        receipt = cursor.fetchall()
        cursor.execute("SELECT entity_type, period_type, total_cost_usd, request_count FROM budget_usage ORDER BY entity_type, period_type")
        debits = cursor.fetchall()
        cursor.execute("SELECT user_id, cost_usd, input_tokens, output_tokens FROM usage_logs WHERE request_id=%s", (request_id,))
        logs = cursor.fetchall()
    return attempt, receipt, debits, logs


async def test_actual_above_request_estimate_settles_in_full_and_blocks_further_spend(metering_db, journal):
    admission, engine = admission_for(metering_db, journal)
    caller = context()
    caller._policy_quote = replace(
        caller._policy_quote, total_usd=Decimal("0.001"), upper_bound_usd=Decimal("0.6"), input_count_method="request_bytes_with_margin"
    )
    try:
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
        attempt, receipt, decision = await complete(admission, caller, "estimated", usage={"input_tokens": 10000, "output_tokens": 2})
        assert attempt.quote.total_usd < decision.ledger_cost <= attempt.quote.maximum_cost_usd
        assert await admission.settle_receipt(attempt, receipt)
        assert ledger(metering_db, "estimated")[1] == [(decision.ledger_cost, 10002)]
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, caller, "after-actual")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("initial_state", ["dispatch_armed", "unknown"])
async def test_competing_receipts_settle_once_and_keep_historical_allocation(metering_db, journal, initial_state):
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt, receipt, decision = await complete(admission, context(), "competing")
        if initial_state == "unknown":
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='competing'")
        with patch("src.budget.pricing_v2_reader.cached_rate_state", side_effect=AssertionError("settlement must not reprice")):
            results = await asyncio.gather(admission.settle_receipt(attempt, receipt), admission.settle_receipt(attempt, receipt))
        assert sorted(results) == [False, True]
        state, claimed, debits, logs = ledger(metering_db, "competing")
        assert state == ("settled", receipt.version_id)
        assert claimed == [(decision.ledger_cost, 3)]
        assert len(debits) == 9
        assert all(total == decision.ledger_cost and count == 1 for _, _, total, count in debits)
        assert logs == [("owner-1", decision.ledger_cost, 1, 2)]
        assert await admission.settle_receipt(attempt, receipt) is False
        assert ledger(metering_db, "competing")[2:] == (debits, logs)
    finally:
        await engine.dispose()


async def test_failed_diagnostic_write_rolls_back_debit_and_armed_hold(metering_db, journal, monkeypatch):
    from src.usage.service import UsageService

    admission, engine = admission_for(metering_db, journal)
    try:
        attempt, receipt, decision = await complete(admission, context(), "rollback")
        original = UsageService.log_request

        async def interrupted(*args, **kwargs):
            raise RuntimeError("diagnostic writer unavailable")

        monkeypatch.setattr(UsageService, "log_request", interrupted)
        with pytest.raises(RuntimeError, match="unavailable"):
            await admission.settle_receipt(attempt, receipt)
        assert ledger(metering_db, "rollback") == (("dispatch_armed", receipt.version_id), [], [], [])
        monkeypatch.setattr(UsageService, "log_request", original)
        assert await admission.settle_receipt(attempt, receipt)
        assert ledger(metering_db, "rollback")[1] == [(decision.ledger_cost, 3)]
    finally:
        await engine.dispose()


async def test_cross_tenant_and_conflicting_final_versions_cannot_charge(metering_db, journal):
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt, receipt, decision = await complete(admission, context(), "protected")
        foreign = replace(receipt, tenant_id="other-tenant")
        with pytest.raises(ValueError, match="invalid final metering object reference"):
            await admission.settle_receipt(attempt, foreign)
        changed = replace(receipt, sha256="0" * 64)
        with pytest.raises(ValueError, match="checksum"):
            await admission.settle_receipt(attempt, changed)
        assert ledger(metering_db, "protected") == (("dispatch_armed", receipt.version_id), [], [], [])
        assert await admission.settle_receipt(attempt, receipt)
        assert ledger(metering_db, "protected")[1] == [(decision.ledger_cost, 3)]
    finally:
        await engine.dispose()


async def test_existing_conflicting_debit_preserves_pending_hold_and_does_not_log(metering_db, journal):
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt, receipt, _decision = await complete(admission, context(), "claimed-elsewhere")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO budget_settlement_receipts (org_id, request_id, user_id, cost_usd, total_tokens, allocation_key) "
                "VALUES ('tenant-1', 'claimed-elsewhere', 'owner-1', 0.99, 3, %s)",
                ("0" * 64,),
            )
        with pytest.raises(ValueError, match="conflicting settlement replay"):
            await admission.settle_receipt(attempt, receipt)
        state, existing, debits, logs = ledger(metering_db, "claimed-elsewhere")
        assert state == ("dispatch_armed", receipt.version_id)
        assert existing == [(Decimal("0.990000"), 3)]
        assert debits == [] and logs == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("stream", [False, True])
async def test_responses_service_hands_off_receipt_and_commits_one_charge(metering_db, journal, stream, monkeypatch):
    from src.proxy.bedrock_enforcement import RoutingDecision
    from src.proxy.bedrock_routing import BedrockTarget
    from src.proxy.mantle_service import drain_usage_finalizers

    routing_stub = AsyncMock(return_value=RoutingDecision(target=BedrockTarget(account_id=None, rung="platform")))
    monkeypatch.setattr("src.proxy.mantle_service.resolve_routing_decision", routing_stub)

    caller = context()
    body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20, "stream": stream}).encode()
    caller._policy_quote = quote(body)
    calls = []

    def provider(request):
        calls.append(request)
        if stream:
            event = {"type": "response.completed", "response": {"usage": USAGE}}
            return httpx.Response(200, content=b"event: response.completed\ndata: " + json.dumps(event).encode() + b"\n\n")
        return httpx.Response(200, json={"usage": USAGE})

    service, admission, engine, _route = service_for(metering_db, journal, caller, provider)
    assert service.create_response.__func__.__globals__["resolve_routing_decision"] is routing_stub
    try:
        async with service._http_client:
            response = await service.create_response(body, caller, stream=stream, model=MODEL, request_id=f"handed-off-{stream}")
            if stream:
                assert b"response.completed" in b"".join([chunk async for chunk in response])
            else:
                assert response.status_code == 200
            assert await drain_usage_finalizers(timeout_seconds=3) == 0
        assert len(calls) == 1
        state, claimed, debits, logs = ledger(metering_db, f"handed-off-{stream}")
        assert state[0] == "settled"
        assert len(claimed) == 1 and len(debits) == 9 and len(logs) == 1
        assert service._log_usage.await_count == 0
    finally:
        await engine.dispose()


@pytest.mark.parametrize("entity_type", ["org", "service_account"])
async def test_settled_user_debit_counts_toward_service_account_cap(metering_db, journal, entity_type):
    caller = context()
    caller._policy_quote = replace(quote(BODY), total_usd=Decimal("0.01"))
    identity = caller.org_id if entity_type == "org" else caller.user_id
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, caller.org_id, entity_type, identity, Decimal("0.01"))
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt, receipt, decision = await complete(admission, caller, f"cap-first-{entity_type}")
        assert decision.ledger_cost > 0
        assert await admission.settle_receipt(attempt, receipt)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT entity_type, total_cost_usd FROM budget_usage WHERE org_id = %s AND entity_id = %s AND period_type = 'daily'",
                (caller.org_id, identity),
            )
            usage_rows = cursor.fetchall()
            assert ("user" if entity_type == "service_account" else "org", decision.ledger_cost) in usage_rows
            assert all(kind != "service_account" for kind, _ in usage_rows)
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, caller, f"cap-second-{entity_type}")
        assert ledger(metering_db, f"cap-first-{entity_type}")[0][0] == "settled"
    finally:
        await engine.dispose()
