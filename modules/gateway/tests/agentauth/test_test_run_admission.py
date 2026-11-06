"""Offline faults with real Dynamo transactions (moto) and reservation Lua.

Identity and pricing below are explicit substituted server adapters. These tests
neither attest an installed native runtime nor provide a live quote or fixture.
"""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import boto3
import fakeredis.aioredis
import pytest
from botocore.exceptions import EndpointConnectionError
from moto import mock_aws

from src.agentauth.test_run_admission import (
    AdmissionReceipt,
    PreparedModelAdmission,
    StrictTestBudgetEnforcementService,
    TestRunAdmissionService,
    VerifiedNativeTestIdentity,
)
from src.agentauth.test_run_admission_store import AdmissionRefusedError, TestAdmissionStore, canonical
from src.budget.config import budget_config
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.reservations import ReservationStore, ReservationTarget
from src.orchestration import provider_quotes
from src.orchestration.provider_quotes import ProviderQuote, QuoteReason, refuse
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EnforcementResult, EntityType, PeriodType

BODY = b'{"model":"offline-model","messages":[{"role":"user","content":"hello"}],"max_tokens":16}'
ROUTE = "/v1/messages"


class NoBudgetRows:
    """Explicit offline result for absent hierarchy rows and person-cap sources."""

    def scalar_one_or_none(self):
        return None

    def one(self):
        return (False, False)


class NoBudgetSession:
    async def execute(self, statement):
        return NoBudgetRows()


def put_authority(fixture, document):
    fixture.client.put_item(
        TableName=fixture.store.table,
        Item={
            **fixture.store.authority_key(document["tenant_id"], document["run_id"]),
            "kind": {"S": "tools_test_run_v1"},
            "document": {"S": canonical(document)},
            "dispatch_expires_at": {"N": str(document["dispatch_expires_at"])},
        },
    )


@pytest.fixture
async def admission(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        client.create_table(
            TableName="offline-test-authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        # Moto deep-copies mutable tables during a transaction and is not thread
        # safe. Serialize its individual DB requests, as Dynamo's atomic writes
        # and consistent reads require; do NOT serialize service operations.
        db_lock = threading.RLock()

        def atomic(method):
            def call(**kwargs):
                with db_lock:
                    return method(**kwargs)

            return call

        for method in ("get_item", "put_item", "update_item", "delete_item", "transact_write_items"):
            monkeypatch.setattr(client, method, atomic(getattr(client, method)))
        clock = [datetime(2026, 1, 1, tzinfo=UTC)]
        identity = VerifiedNativeTestIdentity(
            "tenant-one", "jwt-subject", "canonical-human", "team-one", "department-one", str(uuid4()), "credential-one", "a" * 64, "runtime-one"
        )
        authority = {
            **asdict(identity),
            "schema_version": "1",
            "cap_usd": "20",
            "meter_epoch": str(uuid4()),
            "dispatch_expires_at": int(clock[0].timestamp()) + 300,
            "cleanup_expires_at": int(clock[0].timestamp()) + 600,
            "meter_expires_at": int(clock[0].timestamp()) + 86400,
            "state": "active",
            "allowed_routes": [ROUTE],
            "allowed_models": ["offline-model"],
            "revision": "revision-one",
        }
        store = TestAdmissionStore(table_name="offline-test-authority", client=client)
        verifier = SimpleNamespace(verify=AsyncMock(return_value=identity))
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        reservations = ReservationStore(redis_url=None, ttl_seconds=120, client=redis, clock=lambda: clock[0].timestamp())
        budget = StrictTestBudgetEnforcementService()
        budget._reservations = reservations

        @asynccontextmanager
        async def session():
            yield NoBudgetSession()

        monkeypatch.setattr(budget, "_get_session", session)
        monkeypatch.setattr(budget, "prepare_enforcement_context", AsyncMock(return_value=None))
        monkeypatch.setattr(budget, "_note_check_succeeded", AsyncMock())
        monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
        monkeypatch.setattr(budget_config, "budget_check_enabled", True)
        monkeypatch.setattr(budget_config, "budget_run_cap_enabled", False)
        service = TestRunAdmissionService(store=store, budget=budget, identity_verifier=verifier, clock=lambda: clock[0])
        target = service._target(authority)
        # Only the offline fixture simulates already-provisioned state. The
        # implementation must never create this row or zero anchor.
        fixture = SimpleNamespace(
            client=client,
            store=store,
            clock=clock,
            identity=identity,
            authority=authority,
            verifier=verifier,
            redis=redis,
            reservations=reservations,
            budget=budget,
            service=service,
            target=target,
            cost=Decimal("12"),
            quote_calls=0,
        )
        put_authority(fixture, authority)
        assert (await reservations.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])).admitted

        async def quote(body, route, *, now):
            fixture.quote_calls += 1
            return ProviderQuote(
                "offline",
                route,
                "text",
                "offline-model",
                hashlib.sha256(body).hexdigest(),
                "offline-rate-v1",
                "USD",
                now,
                now + timedelta(seconds=120),
                1_050_000,
                16,
                0,
                0,
                fixture.cost,
                "offline-substitute",
            )

        async def revalidate(quote, body, route, *, now):
            if route != quote.endpoint or hashlib.sha256(body).hexdigest() != quote.request_sha256 or quote.expired(now):
                raise refuse(QuoteReason.REQUEST_CHANGED)

        async def confirm(quote, *, now=None):
            return refuse(QuoteReason.QUOTE_EXPIRED).refusal if quote.expired(now or clock[0]) else None

        monkeypatch.setattr(provider_quotes, "quote_request", quote)
        monkeypatch.setattr(provider_quotes, "revalidate_quote", revalidate)
        monkeypatch.setattr(provider_quotes, "confirm_quote_spendable", confirm)
        yield fixture
        await redis.aclose()


def context(admission):
    return TokenContext(
        user_id=admission.identity.subject_id,
        org_id=admission.identity.tenant_id,
        team_id="token-team",
        department_id="token-department",
        account_type="human",
        expires_at=admission.clock[0] + timedelta(hours=1),
    )


async def prepare(admission, *, operation_id=None, **changes):
    request = {
        "context": context(admission),
        "credential": "offline-scoped-credential",
        "operation_id": operation_id or str(uuid4()),
        "body": BODY,
        "route": ROUTE,
        **changes,
    }
    return await admission.service.prepare_model(**request)


async def begin(admission, prepared, **changes):
    return await admission.service.begin_model_dispatch(
        prepared, context=context(admission), credential="offline-scoped-credential", **{"body": BODY, "route": ROUTE, **changes}
    )


async def meter(admission):
    return await admission.redis.hgetall(admission.target.key())


async def test_estimated_quote_ceiling_survives_saved_dispatch(admission, monkeypatch):
    original = provider_quotes.quote_request

    async def estimated(*args, **kwargs):
        quote = await original(*args, **kwargs)
        return replace(quote, upper_bound_usd=Decimal(20), input_count_method="request_bytes_with_margin")

    monkeypatch.setattr(provider_quotes, "quote_request", estimated)
    prepared = await prepare(admission)
    assert prepared._quote.maximum_cost_usd == 20
    await begin(admission, prepared)


async def test_default_native_verifier_is_closed_even_for_human_jwt(admission):
    admission.service.identity_verifier = TestRunAdmissionService(store=admission.store).identity_verifier
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="mandatory_native_test_identity_unavailable"):
        await prepare(admission)
    assert await meter(admission) == before
    assert admission.client.scan(TableName=admission.store.table)["Count"] == 1


async def test_server_quote_is_reserved_in_canonical_hierarchy_context(admission):
    result = await prepare(admission)
    assert isinstance(result, PreparedModelAdmission)
    assert result._context.user_id == "jwt-subject"
    assert result._context.team_id == "team-one" and result._context.department_id == "department-one"
    assert result._quote.max_input_tokens == 1_050_000
    assert result.receipt.reserved_usd == "12"
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 12
    assert result._operation["target_keys"] == [admission.target.key()]
    assert result._operation["binding"]["source_sha256"] == "a" * 64
    assert result._operation["binding"]["user_id"] == "canonical-human"


async def test_same_id_replay_does_not_requote_resize_or_acquire_handoff(admission):
    operation_id = str(uuid4())
    original = await prepare(admission, operation_id=operation_id)
    before = await meter(admission)
    admission.cost = Decimal("1")
    replay = await prepare(admission, operation_id=operation_id)
    assert isinstance(replay, AdmissionReceipt) and replay.replay and replay.reservation_id == original.receipt.reservation_id
    assert admission.quote_calls == 1 and await meter(admission) == before
    with pytest.raises(AdmissionRefusedError, match="requires_fresh_admission"):
        await begin(admission, replay)


async def test_concurrent_same_id_returns_one_fresh_admission(admission):
    operation_id = str(uuid4())
    results = await asyncio.gather(*(prepare(admission, operation_id=operation_id) for _ in range(4)))
    assert sum(isinstance(result, PreparedModelAdmission) for result in results) == 1
    assert admission.quote_calls == 1
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 12


async def test_concurrent_new_operations_share_cap_and_keep_cleanup_composite_hold(admission):
    assert (await admission.reservations.reserve("existing-cleanup-composite", Decimal(6), [admission.target])).admitted
    cleanup_before = (await meter(admission))["existing-cleanup-composite"]
    results = await asyncio.gather(prepare(admission), prepare(admission), return_exceptions=True)
    assert sum(isinstance(result, PreparedModelAdmission) for result in results) == 1
    assert sum(isinstance(result, AdmissionRefusedError) for result in results) == 1
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 18
    assert (await meter(admission))["existing-cleanup-composite"] == cleanup_before


async def test_changed_same_id_payload_is_refused_before_new_quote(admission):
    operation_id = str(uuid4())
    await prepare(admission, operation_id=operation_id)
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="test_operation_conflict"):
        await prepare(admission, operation_id=operation_id, body=BODY + b" ")
    assert await meter(admission) == before and admission.quote_calls == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha256", "b" * 64),
        ("runtime_id", "other-runtime"),
        ("user_id", "foreign-human"),
        ("team_id", "foreign-team"),
        ("subject_id", "foreign-subject"),
        ("tenant_id", "foreign-tenant"),
    ],
)
async def test_foreign_or_changed_verified_binding_refuses(admission, field, value):
    admission.verifier.verify.return_value = replace(admission.identity, **{field: value})
    with pytest.raises(AdmissionRefusedError):
        await prepare(admission)
    assert admission.quote_calls == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("cap_usd", "21"),
        ("cap_usd", "0"),
        ("state", "revoked"),
        ("revision", ""),
        ("dispatch_expires_at", True),
        ("allowed_routes", ["/unsupported"]),
        ("allowed_models", []),
        ("meter_epoch", "invented-epoch"),
    ],
)
async def test_malformed_or_inactive_authority_never_gets_default_permission(admission, field, value):
    changed = {**admission.authority, field: value}
    if field == "dispatch_expires_at":
        # Invalid typed Dynamo expiry is itself a corrupt record.
        changed[field] = 0
    put_authority(admission, changed)
    with pytest.raises(AdmissionRefusedError):
        await prepare(admission)
    assert admission.quote_calls == 0


async def test_missing_authority_or_meter_is_never_created(admission):
    await admission.redis.delete(admission.target.key())
    with pytest.raises(AdmissionRefusedError, match="test_accumulator_unavailable"):
        await prepare(admission)
    assert await meter(admission) == {}
    admission.client.delete_item(
        TableName=admission.store.table, Key=admission.store.authority_key(admission.identity.tenant_id, admission.identity.run_id)
    )
    with pytest.raises(AdmissionRefusedError, match="test_authority_unavailable"):
        await prepare(admission)
    assert await meter(admission) == {}


async def test_existing_delivery_flow_target_is_refused_and_preserved(admission):
    original = context(admission)
    target = replace(admission.target, entity_type="flow", entity_id="accepted-flow", headroom_usd=Decimal(500))
    original._policy_flow_target = target
    with pytest.raises(AdmissionRefusedError, match="incompatible_test_identity_context"):
        await prepare(admission, context=original)
    assert original._policy_flow_target is target
    assert admission.quote_calls == 0


@pytest.mark.parametrize("route", ["/agent/tools/call", "/v1/chat/completions", "/openai/v1/responses", ""])
async def test_tools_unapproved_and_unsupported_routes_refuse_before_quote(admission, route):
    with pytest.raises(AdmissionRefusedError, match="unsupported_test_provider_quote"):
        await prepare(admission, route=route)
    assert admission.quote_calls == 0


async def test_caller_quote_or_authority_boolean_has_no_api(admission):
    with pytest.raises(TypeError):
        await prepare(admission, quote={"total_usd": "0"}, authority_confirmed=True)
    assert admission.quote_calls == 0


async def test_model_allowlist_is_checked_after_server_quote(admission):
    put_authority(admission, {**admission.authority, "allowed_models": ["other-model"]})
    with pytest.raises(AdmissionRefusedError, match="unsupported_test_provider_quote"):
        await prepare(admission)
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 0


async def test_unavailable_registered_quote_never_reserves(admission, monkeypatch):
    monkeypatch.setattr(provider_quotes, "quote_request", AsyncMock(side_effect=refuse(QuoteReason.NO_ADAPTER)))
    with pytest.raises(AdmissionRefusedError, match="test_admission_unavailable"):
        await prepare(admission)
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 0


@pytest.mark.parametrize("control", ["budget_check_enabled", "budget_reservation_enabled", "enforcement_disabled", "accounting_incomplete"])
async def test_enforcement_off_or_unknown_cannot_fall_back(admission, monkeypatch, control):
    if control in {"budget_check_enabled", "budget_reservation_enabled"}:
        monkeypatch.setattr(budget_config, control, False)
    else:

        async def posture(ctx, _run):
            if control == "enforcement_disabled":
                ctx._budget_enforcement_enabled = False
            else:
                ctx._budget_accounting_incomplete = True

        monkeypatch.setattr(admission.budget, "prepare_enforcement_context", posture)
    with pytest.raises(AdmissionRefusedError, match="strict_test_budget_unavailable"):
        await prepare(admission)
    assert admission.quote_calls == 0


async def test_existing_hierarchy_and_test_hold_commit_in_one_transaction(admission, monkeypatch):
    hierarchy = ReservationTarget(admission.identity.tenant_id, "user", admission.identity.subject_id, "daily", "2026-01-01", Decimal(5))
    user_keys, reserve_targets = [], []
    reserve = admission.reservations.reserve

    async def record_reserve(request_id, amount, targets):
        reserve_targets.append([target.key() for target in targets])
        return await reserve(request_id, amount, targets)

    async def check(_session, entity_type, _id, period_type, *_args, **_kwargs):
        if entity_type == EntityType.USER:
            user_keys.append(_id)
        return EnforcementResult(allowed=True), hierarchy if entity_type == EntityType.USER and period_type == PeriodType.DAILY else None

    monkeypatch.setattr(admission.budget, "_check_entity_budget", check)
    monkeypatch.setattr(admission.reservations, "reserve", record_reserve)
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="test_budget_refused"):
        await prepare(admission)
    assert await meter(admission) == before
    assert await admission.redis.hgetall(hierarchy.key()) == {}
    assert set(user_keys) == {admission.identity.subject_id}
    assert reserve_targets == [[admission.target.key(), hierarchy.key()]]


async def test_native_hierarchy_hold_cannot_expire_before_cleanup_or_original_meter(admission, monkeypatch):
    hierarchy = ReservationTarget(admission.identity.tenant_id, "user", admission.identity.subject_id, "daily", "2026-01-01", Decimal(20))

    async def check(session, entity_type, entity_id, period_type, *args, **kwargs):
        return EnforcementResult(allowed=True), hierarchy if entity_type == EntityType.USER and period_type == PeriodType.DAILY else None

    monkeypatch.setattr(admission.budget, "_check_entity_budget", check)
    prepared = await prepare(admission)
    held = next(target for target in prepared._context._budget_admission_targets if target.key() == hierarchy.key())
    assert held.ttl_seconds == admission.target.ttl_seconds
    admission.clock[0] += timedelta(seconds=601)
    assert await admission.service._verify_hold(prepared.receipt.reservation_id, Decimal(12), held) > admission.clock[0].timestamp()
    # The ordinary hierarchy request still counts the native reservation after
    # its usual120-second TTL, independently of the original20USD test meter.
    outcome = await admission.reservations.reserve("ordinary-next", Decimal(9), [hierarchy])
    assert not outcome.admitted


async def test_lost_reservation_reply_retains_unknown_full_hold(admission, monkeypatch):
    original = admission.reservations.reserve

    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("offline lost reply")

    monkeypatch.setattr(admission.reservations, "reserve", lost)
    with pytest.raises(AdmissionRefusedError, match="test_admission_unavailable"):
        await prepare(admission)
    entries = await meter(admission)
    assert any(key.startswith("tools-test-model:") and value.startswith("12:") for key, value in entries.items())
    assert await admission.reservations.snapshot(admission.target) is None


async def test_lost_admitted_journal_reply_has_no_second_owner_or_release(admission, monkeypatch):
    original = admission.store.transition

    def lost(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs["status"] == "admitted":
            raise EndpointConnectionError(endpoint_url="https://offline.invalid")
        return result

    monkeypatch.setattr(admission.store, "transition", lost)
    operation_id = str(uuid4())
    with pytest.raises(AdmissionRefusedError):
        await prepare(admission, operation_id=operation_id)
    replay = await prepare(admission, operation_id=operation_id)
    assert isinstance(replay, AdmissionReceipt) and replay.replay
    assert await admission.reservations.snapshot(admission.target) is None
    assert admission.quote_calls == 1


async def test_begin_dispatch_is_single_use_and_quote_bound(admission):
    prepared = await prepare(admission)
    with pytest.raises(AdmissionRefusedError):
        await begin(admission, prepared, body=BODY + b" ")
    first = await begin(admission, prepared)
    assert first.status == "dispatch_started"
    with pytest.raises(AdmissionRefusedError, match="transition_unconfirmed"):
        await begin(admission, prepared)
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 12


async def test_revocation_and_quote_expiry_stop_handoff_without_releasing_hold(admission):
    prepared = await prepare(admission)
    admission.clock[0] += timedelta(seconds=121)
    with pytest.raises(AdmissionRefusedError):
        await begin(admission, prepared)
    put_authority(admission, {**admission.authority, "state": "revoked"})
    with pytest.raises(AdmissionRefusedError):
        await begin(admission, prepared)
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 12


async def test_lost_handoff_acknowledgement_never_reauthorizes_dispatch(admission, monkeypatch):
    prepared = await prepare(admission)
    original = admission.client.transact_write_items

    def lost(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://offline.invalid")

    monkeypatch.setattr(admission.client, "transact_write_items", lost)
    with pytest.raises(AdmissionRefusedError):
        await begin(admission, prepared)
    monkeypatch.setattr(admission.client, "transact_write_items", original)
    with pytest.raises(AdmissionRefusedError):
        await begin(admission, prepared)
    row = admission.store.read_operation(admission.identity.tenant_id, admission.identity.run_id, prepared.receipt.operation_id)
    assert row["status"] == "dispatch_started"
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 12


async def test_unknown_sibling_blocks_new_model_and_cleanup_remains(admission):
    await admission.reservations.reserve("existing-cleanup-composite", Decimal(3), [admission.target])
    await admission.reservations.mark_unknown("existing-cleanup-composite", admission.target)
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="test_accumulator_unavailable"):
        await prepare(admission)
    assert await meter(admission) == before


async def test_no_settlement_or_meter_reset_api_is_exposed(admission):
    for name in ("settle", "release", "initialize", "enroll", "create_run"):
        assert not hasattr(admission.service, name)
        assert not hasattr(admission.store, name)


def test_legacy_or_substituted_budget_service_cannot_enable_test_admission(admission):
    with pytest.raises(AdmissionRefusedError, match="strict_test_budget_service_required"):
        TestRunAdmissionService(store=admission.store, budget=BudgetEnforcementService())


@pytest.mark.parametrize("failure", [OSError("offline unavailable person ledger"), RuntimeError("offline malformed person ledger")])
async def test_person_layer_failure_is_a_strict_refusal_without_skipping(admission, monkeypatch, caplog, failure):
    monkeypatch.setattr(admission.budget, "_check_person_budget", AsyncMock(side_effect=failure))
    with pytest.raises(AdmissionRefusedError, match="test_budget_refused"):
        await prepare(admission)
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 0
    assert "person_layer_skipped" not in caplog.text and "was SKIPPED" not in caplog.text


@pytest.mark.parametrize("cap,permitted", [(Decimal(5), False), (Decimal(20), True)])
async def test_finite_person_cap_is_evaluated_before_test_reservation(admission, monkeypatch, cap, permitted):
    from src.budget import person_ledger

    limit = person_ledger.PersonLimit("daily", cap, "hard", "own", "offline person limit")
    monkeypatch.setattr(
        admission.budget,
        "_resolve_person_limits",
        AsyncMock(
            return_value=(
                {"daily": limit},
                "users:canonical-human",
                [admission.identity.user_id],
                [admission.identity.subject_id],
                [admission.identity.tenant_id],
            )
        ),
    )
    monkeypatch.setattr(person_ledger, "read_person_partition_spend", AsyncMock(return_value=(Decimal(3), Decimal(1))))
    if permitted:
        assert isinstance(await prepare(admission), PreparedModelAdmission)
    else:
        with pytest.raises(AdmissionRefusedError, match="test_budget_refused"):
            await prepare(admission)
    assert (await admission.reservations.snapshot(admission.target)).total_usd == (12 if permitted else 0)


async def test_accounting_became_incomplete_during_hierarchy_check_retains_hold_and_refuses(admission, monkeypatch):
    original = admission.budget.check_budget_hierarchy

    async def incomplete(ctx, *args, **kwargs):
        result = await original(ctx, *args, **kwargs)
        ctx._budget_accounting_incomplete = True
        return result

    monkeypatch.setattr(admission.budget, "check_budget_hierarchy", incomplete)
    with pytest.raises(AdmissionRefusedError, match="test_budget_refused"):
        await prepare(admission)
    assert await admission.reservations.snapshot(admission.target) is None
    assert any(key.startswith("tools-test-model:") and value.startswith("12:") for key, value in (await meter(admission)).items())
