"""Accounting-side arm and permit checks; physical transport submission belongs to #7033."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
import psycopg2
import pytest

from src.budget.metering_admission import MeteringAccountingUnresolvedError, MeteringAdmissionError, MeteringBudgetExceededError
from src.proxy.bedrock_enforcement import RoutingDecision
from src.proxy.bedrock_routing import BedrockTarget
from src.proxy.metered_transport import MeteredProvenUnsentError
from tests.budget.test_metering_scope_admission import (
    BODY,
    admission_for,
    context,
    insert_budget,
    prepare,
)
from tests.proxy.test_mantle_metering_admission import MODEL, quote, rows, service_for

pytest_plugins = ("tests.budget.test_metering_scope_admission",)

DESTINATION = "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses"


async def arm(admission, attempt):
    return await admission.arm(attempt, submitted=BODY, model=MODEL, destination=DESTINATION)


@pytest.mark.parametrize(
    "change,error",
    [
        ("unknown", MeteringAccountingUnresolvedError),
        ("cap", MeteringBudgetExceededError),
        ("gate_revision", MeteringAdmissionError),
        ("epoch", MeteringAdmissionError),
        ("expired_prepared", MeteringAdmissionError),
    ],
)
async def test_arm_rechecks_affected_scope_after_journal_io(metering_db, journal, change, error):
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt = await prepare(admission, context(), f"arm-{change}")
        if change == "unknown":
            await prepare(admission, context(), "intersecting-unknown")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            if change == "unknown":
                cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='intersecting-unknown'")
            elif change == "cap":
                insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.00"))
            elif change == "gate_revision":
                cursor.execute("UPDATE metering_scope_gates SET revision=revision+1 WHERE scope_kind='org'")
            elif change == "epoch":
                cursor.execute("UPDATE metering_attempts SET producer_epoch=producer_epoch+1 WHERE request_id=%s", (attempt.receipt.request_id,))
            else:
                cursor.execute(
                    "UPDATE metering_attempts SET prepare_deadline=NOW()-interval '1 second' WHERE request_id=%s", (attempt.receipt.request_id,)
                )
        with pytest.raises(error):
            await arm(admission, attempt)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state, dispatch_deadline FROM metering_attempts WHERE request_id=%s", (attempt.receipt.request_id,))
            assert cursor.fetchone() == ("prepared", None)
    finally:
        await engine.dispose()


async def test_arm_rejects_changed_quote_body_model_and_elapsed_deadline(metering_db, journal):
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt = await prepare(admission, context(), "immutable-arm")
        for candidate, submitted, model in (
            (attempt, b"changed", MODEL),
            (attempt, BODY, "another-model"),
            (replace(attempt, quote_sha256="0" * 64), BODY, MODEL),
            (replace(attempt, deadline=asyncio.get_running_loop().time() - 1), BODY, MODEL),
        ):
            with pytest.raises(MeteringAdmissionError):
                await admission.arm(candidate, submitted=submitted, model=model, destination=DESTINATION)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state, dispatch_deadline FROM metering_attempts WHERE request_id='immutable-arm'")
            assert cursor.fetchone() == ("prepared", None)
    finally:
        await engine.dispose()


async def test_one_use_permit_covers_exact_cap_and_preserves_armed_exposure(metering_db, journal):
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
    admission, engine = admission_for(metering_db, journal, max_open_per_scope=1)
    try:
        attempt = await prepare(admission, context(), "one-use")
        with (
            patch("src.budget.metering_admission.datetime", wraps=datetime) as wall_clock,
            patch("src.budget.metering_admission.monotonic", return_value=asyncio.get_running_loop().time()),
        ):
            wall_clock.now.return_value = datetime.now(UTC)
            permit = await arm(admission, attempt)
            assert (permit.tenant_id, permit.request_id, permit.revision, permit.producer_epoch) == ("tenant-1", "one-use", 3, 1)
            permit.consume(submitted=BODY, model=MODEL, destination=DESTINATION, pricing_revision=attempt.quote.pricing_revision)
            with pytest.raises(MeteringAdmissionError, match="already consumed"):
                permit.consume(submitted=BODY, model=MODEL, destination=DESTINATION, pricing_revision=attempt.quote.pricing_revision)
        with pytest.raises(MeteringAdmissionError, match="admission cannot be armed"):
            await arm(admission, attempt)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state, dispatch_deadline FROM metering_attempts WHERE request_id='one-use'")
            state, deadline = cursor.fetchone()
            assert state == "dispatch_armed" and deadline is not None
            cursor.execute("SELECT COUNT(*), SUM(bound_usd) FROM metering_attempt_scopes WHERE request_id='one-use'")
            assert cursor.fetchone() == (3, Decimal("0.018000"))
    finally:
        await engine.dispose()


@pytest.mark.parametrize("mismatch", ["body", "model", "destination", "pricing", "expired"])
async def test_permit_refuses_changed_or_expired_handoff_without_a_second_try(metering_db, journal, monkeypatch, mismatch):
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt = await prepare(admission, context(), f"permit-{mismatch}")
        permit = await arm(admission, attempt)
        arguments = {"submitted": BODY, "model": MODEL, "destination": DESTINATION, "pricing_revision": attempt.quote.pricing_revision}
        if mismatch == "body":
            arguments["submitted"] = b"changed"
        elif mismatch == "model":
            arguments["model"] = "another-model"
        elif mismatch == "destination":
            arguments["destination"] = "https://another.example/openai/v1/responses"
        elif mismatch == "pricing":
            arguments["pricing_revision"] = "new-revision"
        else:
            monkeypatch.setattr("src.budget.metering_admission.monotonic", lambda: permit.monotonic_deadline + 1)
        with pytest.raises(MeteringAdmissionError, match="binding changed"):
            permit.consume(**arguments)
        with pytest.raises(MeteringAdmissionError, match="already consumed"):
            permit.consume(submitted=BODY, model=MODEL, destination=DESTINATION, pricing_revision=attempt.quote.pricing_revision)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state, final_version FROM metering_attempts WHERE request_id=%s", (attempt.receipt.request_id,))
            assert cursor.fetchone() == ("dispatch_armed", None)
            cursor.execute("SELECT COUNT(*) FROM usage_logs WHERE request_id=%s", (attempt.receipt.request_id,))
            assert cursor.fetchone() == (0,)
    finally:
        await engine.dispose()


def test_metered_route_without_physical_transport_fails_closed_before_preparation(metering_db, journal):
    caller = context()
    calls = []
    service, _admission, engine, route = service_for(metering_db, journal, caller, lambda request: calls.append(request) or httpx.Response(200))
    service._metered_transport = None
    decision = RoutingDecision(target=BedrockTarget(account_id=None, rung="platform"))
    try:
        with patch("src.proxy.mantle_service.resolve_routing_decision", new=AsyncMock(return_value=decision)):
            response = route.post("/openai/v1/responses", content=BODY, headers={"Content-Type": "application/json"})
        assert response.status_code == 503
        assert response.json()["error"] == "metering_transport_unavailable"
        assert rows(metering_db) == []
        assert calls == []
    finally:
        asyncio.run(engine.dispose())


async def test_proven_unarmed_release_preserves_receipt_and_does_not_release_armed_work(metering_db, journal):
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
    admission, engine = admission_for(metering_db, journal)
    try:
        first = await prepare(admission, context(), "safe-unarmed")
        await admission.cancel_unarmed(first)
        second = await prepare(admission, context(), "possible-send")
        await arm(admission, second)
        with pytest.raises(MeteringAdmissionError, match="cannot be proved"):
            await admission.cancel_unarmed(second)
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, context(), "still-held")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT state, admission_version FROM metering_attempts WHERE request_id IN ('safe-unarmed','possible-send') ORDER BY request_id"
            )
            states = cursor.fetchall()
            assert [(state, bool(version)) for state, version in states] == [("dispatch_armed", True), ("cancelled_unsubmitted", True)]
            cursor.execute("SELECT COUNT(*) FROM usage_logs WHERE request_id IN ('safe-unarmed','possible-send')")
            assert cursor.fetchone() == (0,)
    finally:
        await engine.dispose()


def test_route_changed_policy_does_not_reach_provider_and_releases_only_unarmed_attempt(metering_db, journal):
    caller = context()
    calls = []
    service, admission, engine, route = service_for(metering_db, journal, caller, lambda request: calls.append(request) or httpx.Response(200))
    original_prepare = admission.prepare

    async def change_policy_after_prepare(**kwargs):
        attempt = await original_prepare(**kwargs)
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_scope_gates SET revision=revision+1 WHERE scope_kind='org'")
        return attempt

    admission.prepare = change_policy_after_prepare
    decision = RoutingDecision(target=BedrockTarget(account_id=None, rung="platform"))
    try:
        with patch("src.proxy.mantle_service.resolve_routing_decision", new=AsyncMock(return_value=decision)):
            response = route.post("/openai/v1/responses", content=BODY, headers={"Content-Type": "application/json"})
        assert response.status_code == 503
        assert response.json()["error"] == "metering_dispatch_unavailable"
        assert calls == []
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state, final_version FROM metering_attempts")
            assert cursor.fetchone() == ("cancelled_unsubmitted", None)
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.parametrize("stream", [False, True])
def test_changed_pricing_at_mock_handoff_retains_unknown_without_provider_or_zero_charge(metering_db, journal, stream):
    caller = context()
    body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20, "stream": stream}).encode()
    caller._policy_quote = quote(body)
    calls = []
    service, _admission, engine, route = service_for(metering_db, journal, caller, lambda request: calls.append(request) or httpx.Response(200))

    class ChangedPricingTransport:
        async def dispatch_once(self, client, request, permit, *, model, stream):
            permit.consume(submitted=request.content, model=model, destination=str(request.url), pricing_revision="new-revision")
            return await client.send(request, stream=stream)

    service._metered_transport = ChangedPricingTransport()
    decision = RoutingDecision(target=BedrockTarget(account_id=None, rung="platform"))
    try:
        with patch("src.proxy.mantle_service.resolve_routing_decision", new=AsyncMock(return_value=decision)):
            response = route.post("/openai/v1/responses", content=body, headers={"Content-Type": "application/json"})
        assert response.status_code == 503
        assert response.json()["error"] == "metering_dispatch_unavailable"
        assert calls == []
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state, bound_usd, final_version FROM metering_attempts")
            assert cursor.fetchone() == ("dispatch_armed", Decimal("0.001000"), None)
            cursor.execute("SELECT COUNT(*) FROM usage_logs")
            assert cursor.fetchone() == (0,)
            cursor.execute("SELECT COUNT(*) FROM budget_usage")
            assert cursor.fetchone() == (0,)
    finally:
        asyncio.run(engine.dispose())


async def test_armed_unsent_transport_proof_durably_cancels_bound(metering_db, journal):
    admission, engine = admission_for(metering_db, journal)
    try:
        attempt = await prepare(admission, context(), "armed-local-unsent")
        permit = await arm(admission, attempt)
        await admission.cancel_armed_unsent(attempt, MeteredProvenUnsentError(permit))
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state, revision, reconciliation_status FROM metering_attempts WHERE request_id='armed-local-unsent'")
            assert cursor.fetchone() == ("cancelled_unsubmitted", 4, "proven_unsubmitted")
        with pytest.raises(MeteringAdmissionError, match="already consumed"):
            permit.consume(submitted=BODY, model=MODEL, destination=DESTINATION, pricing_revision=attempt.quote.pricing_revision)
    finally:
        await engine.dispose()
