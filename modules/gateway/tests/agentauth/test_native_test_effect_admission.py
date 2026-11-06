"""Offline all-in quote/journal boundary, not a production price or fixture.

Every positive identity, price and quantity adapter below is explicitly replaced
for testing. Production composition has neither and must refuse paid effects.
"""

import asyncio
import hashlib
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.native_test_effect_admission import NativeEffectHandoff, NativeEffectSDKExecutor, NativeTestEffectAdmission
from src.agentauth.native_test_effect_contract import ProtectedEffectQuoteAdapter
from src.agentauth.native_test_effect_quotes import COMPONENTS, CostBound, NativeEffectQuote, agentcore_compute_components
from src.agentauth.native_test_enrollment import NativeTestStore
from src.agentauth.test_run_admission import AdmissionReceipt, PreparedModelAdmission
from src.agentauth.test_run_admission_store import AdmissionRefusedError, digest
from tests.agentauth import test_test_run_admission as primitive

admission = primitive.admission
EFFECT = "native-effect:offline.session"
BODY = b'{"provider":"offline","session_seconds":300}'


@pytest.fixture
def effect(admission):
    f = admission
    state = {"cost": "6", "revision": "a" * 64}
    verified = AsyncMock(return_value=f.identity)
    verifier = SimpleNamespace(verify_effect=verified)

    async def quote(body, effect_key, authority, *, now):
        # Explicitly synthetic complete terms, including zero-cost proofs. The
        # real source adapter must prove every quantity and price separately.
        terms = tuple(
            CostBound(name, "offline-unit", "1" if name == "provider_compute" else "0", state["cost"], "b" * 64, "c" * 64)
            for name in sorted(COMPONENTS)
        )
        return NativeEffectQuote(
            effect_key=effect_key,
            request_sha256=hashlib.sha256(body).hexdigest(),
            authority_sha256=digest(authority),
            source_contract_sha256="d" * 64,
            pricing_revision=state["revision"],
            issued_at=now,
            expires_at=now + timedelta(seconds=120),
            session_seconds=300,
            cleanup_seconds=600,
            cleanup_attempts=2,
            components=terms,
            total_usd=Decimal(state["cost"]),
        )

    async def confirm(quote, *, now):
        if quote.pricing_revision != state["revision"] or quote.expired(now):
            raise AdmissionRefusedError("offline_price_changed")

    async def revalidate(quote, body, authority, *, now):
        quote.require_binding(body, EFFECT, authority, now)
        await confirm(quote, now=now)

    adapter = SimpleNamespace(quote=quote, revalidate=revalidate, confirm=confirm)
    service = NativeTestEffectAdmission(
        effect_key=EFFECT,
        store=f.store,
        budget=f.budget,
        identity_verifier=verifier,
        quote_adapter=adapter,
        clock=lambda: f.clock[0],
    )
    return SimpleNamespace(f=f, service=service, adapter=adapter, verifier=verifier, state=state, context=primitive.context(f))


async def prepare(e, *, operation_id=None, body=BODY):
    return await e.service.prepare_effect(context=e.context, credential="offline-all-in", operation_id=operation_id or str(uuid4()), body=body)


def handoff(e, prepared):
    return NativeEffectHandoff(e.service, prepared, e.context, "offline-all-in", BODY)


def sdk(attempts=1):
    return SimpleNamespace(meta=SimpleNamespace(config=SimpleNamespace(retries={"total_max_attempts": attempts})))


async def test_model_and_complete_paid_lifecycle_share_one_twenty_dollar_meter(effect):
    e = effect
    model = await e.f.service.prepare_model(
        context=e.context, credential="offline-model", operation_id=str(uuid4()), body=primitive.BODY, route=primitive.ROUTE
    )
    paid = await prepare(e)
    assert isinstance(paid, PreparedModelAdmission)
    assert model._context._policy_flow_target.key() == paid._context._policy_flow_target.key()
    assert paid.receipt.reserved_usd == "6"
    assert (await e.f.reservations.snapshot(e.f.target)).total_usd == 18
    operation = e.f.store.read_operation("tenant-one", e.f.identity.run_id, paid.receipt.operation_id)
    assert {term["component"] for term in operation["quote"]["components"]} == COMPONENTS
    assert operation["quote"]["cleanup_seconds"] == 600
    with pytest.raises(AdmissionRefusedError, match="budget_refused"):
        await prepare(e)
    assert (await e.f.reservations.snapshot(e.f.target)).total_usd == 18


async def test_real_journal_cas_precedes_one_provider_send_and_replay_is_status_only(effect):
    e = effect
    prepared = await prepare(e)
    transfer = handoff(e, prepared)
    with pytest.raises(AdmissionRefusedError):
        transfer.before_submit()
    await transfer.dispatch(body=BODY, request_id=prepared.receipt.reservation_id, client=sdk())
    operation = e.f.store.read_operation("tenant-one", e.f.identity.run_id, prepared.receipt.operation_id)
    assert operation["status"] == "dispatch_started"
    calls = []

    def provider():
        transfer.before_submit()
        calls.append("paid effect")

    provider()
    with pytest.raises(AdmissionRefusedError):
        provider()
    assert calls == ["paid effect"]
    replay = await prepare(e, operation_id=prepared.receipt.operation_id)
    assert isinstance(replay, AdmissionReceipt) and replay.replay
    with pytest.raises(AdmissionRefusedError):
        await handoff(e, prepared).dispatch(body=BODY, request_id=prepared.receipt.reservation_id, client=sdk())
    with pytest.raises(AdmissionRefusedError):
        handoff(e, prepared).before_submit()
    # No response or provider observation becomes a settlement or cleanup release.
    assert (await e.f.reservations.snapshot(e.f.target)).total_usd == 6


@pytest.mark.parametrize("change", ["body", "request_id", "sdk_retries", "sdk_boolean_attempts", "price", "clock", "target"])
async def test_provider_binding_changes_preserve_hold_and_refuse_dispatch(effect, change):
    e = effect
    prepared = await prepare(e)
    body, request_id, client = BODY, prepared.receipt.reservation_id, sdk()
    if change == "body":
        body += b" "
    elif change == "request_id":
        request_id += "x"
    elif change == "sdk_retries":
        client = sdk(2)
    elif change == "sdk_boolean_attempts":
        client = sdk(True)
    elif change == "price":
        e.state["revision"] = "f" * 64
    elif change == "clock":
        e.f.clock[0] += timedelta(seconds=121)
    else:
        prepared._context._budget_admission_targets[0] = replace(prepared._context._budget_admission_targets[0], require_initialization=False)
    with pytest.raises(AdmissionRefusedError):
        await handoff(e, prepared).dispatch(body=body, request_id=request_id, client=client)
    operation = e.f.store.read_operation("tenant-one", e.f.identity.run_id, prepared.receipt.operation_id)
    assert operation["status"] == "admitted"
    assert prepared.receipt.reservation_id in await e.f.redis.hgetall(e.f.target.key())


async def test_price_changed_during_hierarchy_check_is_rejected_before_atomic_reserve(effect, monkeypatch):
    e = effect

    async def changed():
        e.state["revision"] = "f" * 64

    monkeypatch.setattr(e.f.budget, "_note_check_succeeded", changed)
    with pytest.raises(AdmissionRefusedError):
        await prepare(e)
    assert (await e.f.reservations.snapshot(e.f.target)).total_usd == 0


@pytest.mark.parametrize("method", ["revalidate", "confirm"])
async def test_quote_source_returned_refusal_is_not_treated_as_confirmation(effect, monkeypatch, method):
    monkeypatch.setattr(effect.adapter, method, AsyncMock(return_value=False))
    with pytest.raises(AdmissionRefusedError):
        await prepare(effect)
    assert (await effect.f.reservations.snapshot(effect.f.target)).total_usd == 0


async def test_parallel_paid_effects_contend_on_same_atomic_cap(effect):
    results = await asyncio.gather(*(prepare(effect) for _ in range(5)), return_exceptions=True)
    assert sum(isinstance(result, PreparedModelAdmission) for result in results) == 3
    assert (await effect.f.reservations.snapshot(effect.f.target)).total_usd == 18


async def test_lost_dispatch_ack_never_authorizes_submission_or_retry(effect, monkeypatch):
    e = effect
    prepared = await prepare(e)
    original = e.f.client.transact_write_items

    def lost_ack(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="offline")

    monkeypatch.setattr(e.f.client, "transact_write_items", lost_ack)
    transfer = handoff(e, prepared)
    with pytest.raises(AdmissionRefusedError):
        await transfer.dispatch(body=BODY, request_id=prepared.receipt.reservation_id, client=sdk())
    with pytest.raises(AdmissionRefusedError):
        transfer.before_submit()
    assert e.f.store.read_operation("tenant-one", e.f.identity.run_id, prepared.receipt.operation_id)["status"] == "dispatch_started"
    assert (await e.f.reservations.snapshot(e.f.target)).total_usd == 6


async def test_executor_delay_cannot_spend_consumed_expired_quote(effect):
    e = effect
    prepared = await prepare(e)
    transfer = handoff(e, prepared)
    await transfer.dispatch(body=BODY, request_id=prepared.receipt.reservation_id, client=sdk())
    e.f.clock[0] += timedelta(seconds=121)
    with pytest.raises(AdmissionRefusedError):
        transfer.before_submit()


@pytest.mark.parametrize("missing", ["identity", "model_only_identity", "quote"])
async def test_default_or_model_only_adapter_never_admits_paid_effect(effect, missing):
    e = effect
    verifier = {"identity": None, "model_only_identity": e.f.verifier, "quote": e.verifier}[missing]
    service = NativeTestEffectAdmission(
        effect_key=EFFECT,
        store=e.f.store,
        budget=e.f.budget,
        identity_verifier=verifier,
        quote_adapter=None if missing == "quote" else e.adapter,
        clock=lambda: e.f.clock[0],
    )
    with pytest.raises(AdmissionRefusedError, match="all_in"):
        await service.prepare_effect(context=e.context, credential="offline", operation_id=str(uuid4()), body=BODY)
    assert (await e.f.reservations.snapshot(e.f.target)).total_usd == 0


@pytest.mark.parametrize("component", sorted(COMPONENTS))
async def test_each_unpriced_setup_cleanup_or_overhead_component_refuses_complete_quote(effect, component):
    e = effect
    quote = await e.adapter.quote(BODY, EFFECT, e.f.authority, now=e.f.clock[0])
    remaining = tuple(term for term in quote.components if term.component != component)
    with pytest.raises(AdmissionRefusedError, match="incomplete"):
        replace(quote, components=remaining, total_usd=sum((term.total for term in remaining), Decimal(0)))


@pytest.mark.parametrize("family,cpu,memory", [("browser", 1, 4), ("code_interpreter", 2, 8)])
def test_official_resource_formula_rounds_each_component_up_without_claiming_all_in(family, cpu, memory):
    terms = agentcore_compute_components(
        family=family,
        billable_seconds=600,
        cpu_usd_per_hour="0.0895",
        memory_usd_per_gb_hour="0.00945",
        pricing_sha256="a" * 64,
        bound_sha256="b" * 64,
    )
    assert Decimal(terms[0].quantity) == Decimal(600) / 3600 * cpu
    assert Decimal(terms[1].quantity) == Decimal(600) / 3600 * memory
    assert sum((term.total for term in terms), Decimal(0)) >= Decimal(600) / 3600 * (Decimal("0.0895") * cpu + Decimal("0.00945") * memory)
    assert {term.component for term in terms} != COMPONENTS


@pytest.mark.parametrize("family,expected", [("browser", "1.0184"), ("code_interpreter", "2.0368")])
def test_billing_reserve_can_cover_documented_eight_hour_maximum_independent_of_cleanup_deadline(family, expected):
    terms = agentcore_compute_components(
        family=family,
        billable_seconds=28800,
        cpu_usd_per_hour="0.0895",
        memory_usd_per_gb_hour="0.00945",
        pricing_sha256="a" * 64,
        bound_sha256="b" * 64,
    )
    assert sum((term.total for term in terms), Decimal(0)) == Decimal(expected)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-0.001", "1e999999", "1e-999999", "9" * 1000, 1])
def test_malformed_or_unbounded_numeric_terms_refuse(value):
    with pytest.raises(AdmissionRefusedError):
        CostBound("kms", "request", value, "0.01", "a" * 64, "b" * 64)


def executor(effect):
    client = sdk()
    client.meta.service_model = SimpleNamespace(service_name="bedrock-agentcore")
    client.meta.region_name = "us-east-1"
    client.meta.endpoint_url = "https://bedrock-agentcore.us-east-1.amazonaws.com"
    client.start_code_interpreter_session = Mock(return_value={"sessionId": "offline"})
    return NativeEffectSDKExecutor(effect.service, client, "start_code_interpreter_session")


async def protected_source(effect, body):
    e = effect
    quote = await e.adapter.quote(body, EFFECT, e.f.authority, now=e.f.clock[0])
    document = dict(
        schema_version="1.0",
        state="active",
        scope="model_tool_setup_cleanup",
        effect_key=EFFECT,
        request_sha256=hashlib.sha256(body).hexdigest(),
        source_sha256="d" * 64,
        pricing_revision="a" * 64,
        not_before=int(e.f.clock[0].timestamp()) - 1,
        expires_at=int(e.f.clock[0].timestamp()) + 3600,
        quote_seconds=60,
        session_seconds=300,
        cleanup_seconds=600,
        cleanup_attempts=2,
        components=[asdict(term) for term in quote.components],
    )
    store = NativeTestStore(table_name=e.f.store.table, client=e.f.client)
    source = ProtectedEffectQuoteAdapter(
        store=store, deployment_id="offline", contract_id="fixed-sdk-call", contract_sha256=digest(document), source_sha256="d" * 64
    )
    e.f.client.put_item(TableName=store.table, Item=store.item(source.key, "native_test_effect_contract_v1", document))
    e.service.quote_adapter = source
    return source, document


async def test_real_sdk_executor_uses_pinned_protected_source_and_original_payload(effect):
    e = effect
    consumer = executor(e)
    parameters = {"codeInterpreterIdentifier": "offline", "sessionTimeoutSeconds": 300}
    source, _ = await protected_source(e, consumer._body(parameters))
    op = str(uuid4())
    receipt, result = await consumer.invoke(context=e.context, credential="offline", operation_id=op, parameters=parameters)
    assert result == {"sessionId": "offline"}
    consumer.client.start_code_interpreter_session.assert_called_once_with(**parameters)
    operation = e.f.store.read_operation("tenant-one", e.f.identity.run_id, op)
    assert operation["status"] == receipt.status == "dispatch_started"
    assert operation["quote"]["source_contract_sha256"] == source.contract_sha256
    replay, result = await consumer.invoke(context=e.context, credential="offline", operation_id=op, parameters=parameters)
    assert replay.replay and result is None
    consumer.client.start_code_interpreter_session.assert_called_once()
    assert (await e.f.reservations.snapshot(e.f.target)).total_usd == 6


async def test_protected_quote_with_fractional_issue_time_uses_current_cas_clock(effect):
    e = effect
    e.f.clock[0] += timedelta(microseconds=123456)
    consumer = executor(e)
    parameters = {"sessionTimeoutSeconds": 300}
    await protected_source(e, consumer._body(parameters))
    receipt, _ = await consumer.invoke(context=e.context, credential="offline", operation_id=str(uuid4()), parameters=parameters)
    assert receipt.status == "dispatch_started"
    consumer.client.start_code_interpreter_session.assert_called_once_with(**parameters)


@pytest.mark.parametrize("change", ["endpoint", "region", "parameters", "contract", "source"])
async def test_source_or_sdk_envelope_change_cannot_be_priced(effect, change):
    e = effect
    consumer = executor(e)
    parameters = {"sessionTimeoutSeconds": 300}
    source, document = await protected_source(e, consumer._body(parameters))
    if change == "endpoint":
        consumer.client.meta.endpoint_url += "/other"
    elif change == "region":
        consumer.client.meta.region_name = "eu-west-1"
    elif change == "parameters":
        parameters["sessionTimeoutSeconds"] = 900
    elif change == "contract":
        document["components"][0]["quantity"] = "100"
        e.f.client.put_item(TableName=source.store.table, Item=source.store.item(source.key, "native_test_effect_contract_v1", document))
    else:
        source.source_sha256 = "f" * 64
    with pytest.raises(AdmissionRefusedError):
        await consumer.invoke(context=e.context, credential="offline", operation_id=str(uuid4()), parameters=parameters)
    consumer.client.start_code_interpreter_session.assert_not_called()
    assert (await e.f.reservations.snapshot(e.f.target)).total_usd == 0


async def test_contract_revocation_at_final_dynamo_cas_refuses_actual_sdk_send(effect, monkeypatch):
    e = effect
    consumer = executor(e)
    parameters = {"sessionTimeoutSeconds": 300}
    source, document = await protected_source(e, consumer._body(parameters))
    original = e.f.client.transact_write_items

    def revoke(**kwargs):
        if any(item.get("Put", {}).get("Item", {}).get("status", {}).get("S") == "dispatch_started" for item in kwargs["TransactItems"]):
            document["state"] = "revoked"
            e.f.client.put_item(TableName=source.store.table, Item=source.store.item(source.key, "native_test_effect_contract_v1", document))
        return original(**kwargs)

    monkeypatch.setattr(e.f.client, "transact_write_items", revoke)
    with pytest.raises(AdmissionRefusedError):
        await consumer.invoke(context=e.context, credential="offline", operation_id=str(uuid4()), parameters=parameters)
    consumer.client.start_code_interpreter_session.assert_not_called()
    assert (await e.f.reservations.snapshot(e.f.target)).total_usd == 6
