"""Durable authority/CAS boundaries for the dormant test admission primitive."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from botocore.exceptions import EndpointConnectionError

from src.agentauth.test_run_admission import AdmissionReceipt
from src.agentauth.test_run_admission_store import AdmissionRefusedError
from src.budget.reservations import ReservationTarget
from src.orchestration import provider_quotes
from src.shared.schemas.budget import EnforcementResult, EntityType, PeriodType
from tests.agentauth.test_test_run_admission import (
    BODY,
    ROUTE,
    admission,  # noqa: F401
    begin,
    context,
    meter,
    prepare,
    put_authority,
)


async def test_authority_change_during_claim_is_atomically_refused(admission, monkeypatch):  # noqa: F811
    original = admission.client.transact_write_items

    def race(**kwargs):
        put_authority(admission, {**admission.authority, "revision": "new-revision"})
        return original(**kwargs)

    monkeypatch.setattr(admission.client, "transact_write_items", race)
    with pytest.raises(AdmissionRefusedError, match="claim_unavailable"):
        await prepare(admission)
    assert admission.client.scan(TableName=admission.store.table)["Count"] == 1
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 0


async def test_stale_owner_cannot_consume_handoff(admission):  # noqa: F811
    prepared = await prepare(admission)
    foreign = {**prepared._operation, "owner_token": "stale-owner"}
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="transition_unconfirmed"):
        admission.store.transition(admission.authority, foreign, status="dispatch_started", now=int(admission.clock[0].timestamp()))
    assert (await begin(admission, prepared)).status == "dispatch_started"
    assert await meter(admission) == before


async def test_lost_claim_reply_returns_existing_without_ownership(admission, monkeypatch):  # noqa: F811
    original = admission.client.transact_write_items

    def lost(**kwargs):
        original(**kwargs)
        raise EndpointConnectionError(endpoint_url="https://offline.invalid")

    monkeypatch.setattr(admission.client, "transact_write_items", lost)
    operation_id = str(uuid4())
    result = await prepare(admission, operation_id=operation_id)
    assert isinstance(result, AdmissionReceipt) and result.replay and result.status == "preparing"
    assert result.reservation_id and result.quoted_usd is None and result.reserved_usd is None
    assert admission.quote_calls == 0
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 0


async def test_corrupt_operation_binding_never_returns_an_authoritative_receipt(admission):  # noqa: F811
    prepared = await prepare(admission)
    key = admission.store.operation_key(admission.identity.tenant_id, admission.identity.run_id, prepared.receipt.operation_id)
    admission.client.update_item(
        TableName=admission.store.table,
        Key=key,
        UpdateExpression="SET binding_digest = :changed",
        ExpressionAttributeValues={":changed": {"S": "forged-binding"}},
    )
    with pytest.raises(AdmissionRefusedError, match="test_operation_unavailable"):
        await prepare(admission, operation_id=prepared.receipt.operation_id)


async def test_read_failure_never_falls_back_to_caller_identity(admission, monkeypatch):  # noqa: F811
    def unavailable(**kwargs):
        raise EndpointConnectionError(endpoint_url="https://offline.invalid")

    monkeypatch.setattr(admission.client, "get_item", unavailable)
    with pytest.raises(AdmissionRefusedError, match="test_authority_unavailable"):
        await prepare(admission)
    assert admission.quote_calls == 0


async def test_no_default_for_missing_authority_field(admission):  # noqa: F811
    document = dict(admission.authority)
    del document["meter_epoch"]
    put_authority(admission, document)
    with pytest.raises(AdmissionRefusedError, match="invalid_test_run_authority"):
        await prepare(admission)
    assert admission.quote_calls == 0


async def test_meter_loss_between_admission_and_handoff_is_not_reset(admission):  # noqa: F811
    prepared = await prepare(admission)
    await admission.redis.delete(admission.target.key())
    with pytest.raises(AdmissionRefusedError, match="test_reservation_unconfirmed"):
        await begin(admission, prepared)
    assert await meter(admission) == {}
    row = admission.store.read_operation(admission.identity.tenant_id, admission.identity.run_id, prepared.receipt.operation_id)
    assert row["status"] == "admitted"


async def test_already_settled_full_cap_does_not_get_another_allowance(admission):  # noqa: F811
    await admission.reservations.reserve("earlier-operation", Decimal(20), [admission.target])
    await admission.reservations.reconcile("earlier-operation", Decimal(20), [admission.target])
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="test_budget_refused"):
        await prepare(admission)
    assert await meter(admission) == before


@pytest.mark.parametrize("credential", ["", None, True])
async def test_missing_scoped_credential_is_refused_before_verifier(admission, credential):  # noqa: F811
    with pytest.raises(AdmissionRefusedError, match="incompatible_test_identity_context"):
        await prepare(admission, credential=credential)
    admission.verifier.verify.assert_not_awaited()


async def test_unknown_reservation_does_not_claim_quote_as_confirmed_reserved(admission, monkeypatch):  # noqa: F811
    original = admission.reservations.reserve

    async def lost(*args, **kwargs):
        await original(*args, **kwargs)
        raise OSError("offline lost response")

    monkeypatch.setattr(admission.reservations, "reserve", lost)
    operation_id = str(uuid4())
    with pytest.raises(AdmissionRefusedError):
        await prepare(admission, operation_id=operation_id)
    receipt = await prepare(admission, operation_id=operation_id)
    assert receipt.status == "unknown" and receipt.reservation_id
    assert receipt.quoted_usd == "12" and receipt.reserved_usd is None
    assert (await meter(admission))[receipt.reservation_id].startswith("12:")


async def test_already_settled_own_field_cannot_be_reused_as_dispatch_ticket(admission):  # noqa: F811
    prepared = await prepare(admission)
    await admission.reservations.reconcile(prepared.receipt.reservation_id, Decimal(12), [admission.target])
    with pytest.raises(AdmissionRefusedError, match="test_reservation_unconfirmed"):
        await begin(admission, prepared)
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 12


async def test_authority_change_while_quote_is_obtained_prevents_reservation(admission, monkeypatch):  # noqa: F811
    from src.orchestration import provider_quotes

    original = provider_quotes.quote_request

    async def race(*args, **kwargs):
        quote = await original(*args, **kwargs)
        put_authority(admission, {**admission.authority, "revision": "changed-after-quote"})
        return quote

    monkeypatch.setattr(provider_quotes, "quote_request", race)
    with pytest.raises(AdmissionRefusedError, match="test_authority_changed"):
        await prepare(admission)
    assert (await admission.reservations.snapshot(admission.target)).total_usd == 0


async def with_hierarchy(admission, monkeypatch):  # noqa: F811
    hierarchy = ReservationTarget(admission.identity.tenant_id, "user", admission.identity.subject_id, "daily", "2026-01-01", Decimal(20))

    async def check(_session, entity_type, _id, period_type, *_args, **_kwargs):
        return EnforcementResult(allowed=True), hierarchy if entity_type == EntityType.USER and period_type == PeriodType.DAILY else None

    monkeypatch.setattr(admission.budget, "_check_entity_budget", check)
    return hierarchy, await prepare(admission)


async def test_ordinary_hierarchy_hold_needs_no_strict_anchor_or_pending_marker(admission, monkeypatch):  # noqa: F811
    hierarchy, prepared = await with_hierarchy(admission, monkeypatch)
    assert await admission.redis.hgetall(hierarchy.key()) == {
        prepared.receipt.reservation_id: f"12:{int(admission.clock[0].timestamp()) + admission.target.ttl_seconds}"
    }
    assert (await begin(admission, prepared)).status == "dispatch_started"


@pytest.mark.parametrize("tamper", ["key", "field", "expired", "amount", "nonfinite", "unknown"])
async def test_every_original_hierarchy_hold_must_still_be_exact_and_live(admission, monkeypatch, tamper):  # noqa: F811
    hierarchy, prepared = await with_hierarchy(admission, monkeypatch)
    reservation_id = prepared.receipt.reservation_id
    key = hierarchy.key()
    if tamper == "key":
        await admission.redis.delete(key)
    elif tamper == "field":
        await admission.redis.hdel(key, reservation_id)
    elif tamper == "unknown":
        await admission.redis.hset(key, "unbounded:" + reservation_id, "0:0")
    else:
        value = {"expired": "12:0", "amount": "1:9999999999", "nonfinite": "12:inf"}[tamper]
        await admission.redis.hset(key, reservation_id, value)
    before = await admission.redis.hgetall(key)
    with pytest.raises(AdmissionRefusedError, match="test_reservation_unconfirmed"):
        await begin(admission, prepared)
    assert await admission.redis.hgetall(key) == before
    row = admission.store.read_operation(admission.identity.tenant_id, admission.identity.run_id, prepared.receipt.operation_id)
    assert row["status"] == "admitted"


@pytest.mark.parametrize("tamper", ["context", "context_and_local_journal"])
async def test_original_target_cannot_be_dropped_from_in_process_handoff(admission, monkeypatch, tamper):  # noqa: F811
    _, prepared = await with_hierarchy(admission, monkeypatch)
    prepared._context._budget_admission_targets = [admission.target]
    if tamper == "context_and_local_journal":
        prepared._operation["target_keys"] = [admission.target.key()]
    with pytest.raises(AdmissionRefusedError):
        await begin(admission, prepared)


async def test_quote_expiring_during_hold_read_refuses_without_consuming_intent(admission, monkeypatch):  # noqa: F811
    prepared = await prepare(admission)
    admission.clock[0] += timedelta(seconds=119)
    original = admission.service._verify_hold

    async def delayed(*args):
        deadline = await original(*args)
        admission.clock[0] += timedelta(seconds=2)
        return deadline

    monkeypatch.setattr(admission.service, "_verify_hold", delayed)
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="test_quote_unavailable"):
        await begin(admission, prepared)
    assert await meter(admission) == before
    assert (
        admission.store.read_operation(admission.identity.tenant_id, admission.identity.run_id, prepared.receipt.operation_id)["status"] == "admitted"
    )


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    "marker,value",
    [
        ("unbounded:", "0:0"),
        ("unbounded:", "0:9999999999"),
        ("pending:", "0:0"),
        ("pending:", "0:inf"),
        ("pending:", "NaN:9999999999"),
        ("pending:", "malformed"),
        ("pending:", "0:9999999999"),
    ],
)
async def test_uncertain_sibling_blocks_handoff_on_every_original_ledger(admission, monkeypatch, strict, marker, value):  # noqa: F811
    hierarchy, prepared = await with_hierarchy(admission, monkeypatch)
    target = admission.target if strict else hierarchy
    await admission.redis.hset(target.key(), marker + "other-operation", value)
    before = await admission.redis.hgetall(target.key())
    with pytest.raises(AdmissionRefusedError, match="test_reservation_unconfirmed"):
        await begin(admission, prepared)
    assert await admission.redis.hgetall(target.key()) == before


async def test_in_process_target_cannot_downgrade_the_durable_strict_guard(admission):  # noqa: F811
    prepared = await prepare(admission)
    await admission.redis.hdel(admission.target.key(), "__initialized__", "pending:" + prepared.receipt.reservation_id)
    prepared._context._budget_admission_targets = [replace(admission.target, require_initialization=False)]
    with pytest.raises(AdmissionRefusedError, match="test_reservation_unconfirmed"):
        await begin(admission, prepared)


async def test_receipt_cannot_borrow_a_sibling_reservation_after_its_own_was_lost(admission):  # noqa: F811
    admission.cost = Decimal(5)
    prepared, sibling = await prepare(admission), await prepare(admission)
    await admission.redis.hdel(admission.target.key(), prepared.receipt.reservation_id, "pending:" + prepared.receipt.reservation_id)
    changed = replace(prepared, receipt=replace(prepared.receipt, reservation_id=sibling.receipt.reservation_id))
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="test_dispatch_binding_changed"):
        await begin(admission, changed)
    assert await meter(admission) == before
    assert (await begin(admission, sibling)).status == "dispatch_started"


@pytest.mark.parametrize("field", ["headroom_usd", "ttl_seconds", "require_initialization"])
async def test_target_metadata_with_the_same_redis_key_must_match_durable_admission(admission, field):  # noqa: F811
    prepared = await prepare(admission)
    values = {"headroom_usd": Decimal(50), "ttl_seconds": 1, "require_initialization": False}
    changed = replace(admission.target, **{field: values[field]})
    prepared._context._budget_admission_targets = [changed]
    prepared._context._policy_flow_target = changed
    with pytest.raises(AdmissionRefusedError, match="test_reservation_unconfirmed"):
        await begin(admission, prepared)


@pytest.mark.parametrize("field", ["quote", "request_id", "subject", "team", "estimated_cost"])
async def test_mutated_prepared_context_cannot_change_provider_usage_binding(admission, field):  # noqa: F811
    prepared = await prepare(admission)
    if field == "quote":
        prepared = replace(prepared, _quote=replace(prepared._quote, total_usd=Decimal(1)))
    else:
        attr, value = {
            "request_id": ("_policy_request_id", "sibling"),
            "subject": ("user_id", "another-subject"),
            "team": ("team_id", "another-team"),
            "estimated_cost": ("_policy_estimated_cost", Decimal(1)),
        }[field]
        setattr(prepared._context, attr, value)
    with pytest.raises(AdmissionRefusedError):
        await begin(admission, prepared)


@pytest.mark.parametrize("delay_at", ["pricing_confirmation", "thread_pool_queue", "cas_response"])
async def test_quote_freshness_after_every_await_before_returning_dispatch_authority(admission, monkeypatch, delay_at):  # noqa: F811
    from src.agentauth import test_run_admission

    prepared = await prepare(admission)
    admission.clock[0] += timedelta(seconds=119)
    if delay_at == "pricing_confirmation":
        confirm = provider_quotes.confirm_quote_spendable

        async def delayed_confirm(*args, **kwargs):
            result = await confirm(*args, **kwargs)
            admission.clock[0] += timedelta(seconds=2)
            return result

        monkeypatch.setattr(provider_quotes, "confirm_quote_spendable", delayed_confirm)
    elif delay_at == "thread_pool_queue":
        in_pool = test_run_admission.run_in_threadpool

        async def delayed_queue(func, *args, **kwargs):
            if func.__name__ == "consume_intent":
                admission.clock[0] += timedelta(seconds=2)
            return await in_pool(func, *args, **kwargs)

        monkeypatch.setattr(test_run_admission, "run_in_threadpool", delayed_queue)
    else:
        transition = admission.store.transition

        def delayed_transition(*args, **kwargs):
            result = transition(*args, **kwargs)
            if kwargs["status"] == "dispatch_started":
                admission.clock[0] += timedelta(seconds=2)
            return result

        monkeypatch.setattr(admission.store, "transition", delayed_transition)
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="test_quote_unavailable"):
        await begin(admission, prepared)
    assert await meter(admission) == before
    row = admission.store.read_operation(admission.identity.tenant_id, admission.identity.run_id, prepared.receipt.operation_id)
    assert row["status"] == ("dispatch_started" if delay_at == "cas_response" else "admitted")
    if delay_at == "cas_response":
        assert (await prepare(admission, operation_id=prepared.receipt.operation_id)).replay
        with pytest.raises(AdmissionRefusedError, match="transition_unconfirmed"):
            await begin(admission, prepared)


async def test_complete_journal_document_is_guarded_during_dispatch_cas(admission, monkeypatch):  # noqa: F811
    prepared = await prepare(admission)
    transition = admission.store.transition

    def race(authority, operation, **kwargs):
        if kwargs["status"] == "dispatch_started":
            changed = {**operation, "targets": [{**operation["targets"][0], "require_initialization": False}]}
            admission.client.put_item(TableName=admission.store.table, Item=admission.store._item(changed))
        return transition(authority, operation, **kwargs)

    monkeypatch.setattr(admission.store, "transition", race)
    with pytest.raises(AdmissionRefusedError, match="transition_unconfirmed"):
        await begin(admission, prepared)
    assert (
        admission.store.read_operation(admission.identity.tenant_id, admission.identity.run_id, prepared.receipt.operation_id)["status"] == "admitted"
    )


@pytest.mark.parametrize("expiry", ["authority", "credential"])
async def test_late_successful_intent_cannot_handoff_after_identity_scope_expires(admission, monkeypatch, expiry):  # noqa: F811
    if expiry == "authority":
        put_authority(admission, {**admission.authority, "dispatch_expires_at": int(admission.clock[0].timestamp()) + 1})
    prepared = await prepare(admission)
    native_context = context(admission)
    if expiry == "credential":
        native_context.expires_at = admission.clock[0] + timedelta(seconds=1)
    transition = admission.store.transition

    def delayed(*args, **kwargs):
        result = transition(*args, **kwargs)
        if kwargs["status"] == "dispatch_started":
            admission.clock[0] += timedelta(seconds=2)
        return result

    monkeypatch.setattr(admission.store, "transition", delayed)
    before = await meter(admission)
    with pytest.raises(AdmissionRefusedError, match="test_dispatch_binding_changed"):
        await admission.service.begin_model_dispatch(prepared, context=native_context, credential="offline-scoped-credential", body=BODY, route=ROUTE)
    assert await meter(admission) == before
    assert (
        admission.store.read_operation(admission.identity.tenant_id, admission.identity.run_id, prepared.receipt.operation_id)["status"]
        == "dispatch_started"
    )


@pytest.mark.parametrize("delay_at", ["none", "pricing_confirmation", "thread_pool_queue", "cas_response"])
@pytest.mark.parametrize("expires", ["own_hierarchy_hold", "sibling_hold", "sibling_pending"])
async def test_verified_ledger_deadline_is_carried_through_later_awaits(admission, monkeypatch, delay_at, expires):  # noqa: F811
    from src.agentauth import test_run_admission

    hierarchy, prepared = await with_hierarchy(admission, monkeypatch)
    if expires == "own_hierarchy_hold":
        # Native holds intentionally override the ordinary short TTL. Inject a
        # shortened stored deadline to retain this later-await race regression.
        await admission.redis.hset(hierarchy.key(), prepared.receipt.reservation_id, f"12:{admission.clock[0].timestamp() + 1}")
    else:
        early, later = admission.clock[0].timestamp() + 1, admission.clock[0].timestamp() + 120
        await admission.redis.hset(
            hierarchy.key(),
            mapping={
                "sibling": f"1:{early if expires == 'sibling_hold' else later}",
                "pending:sibling": f"0:{early if expires == 'sibling_pending' else later}",
            },
        )
    if delay_at == "pricing_confirmation":
        confirm = provider_quotes.confirm_quote_spendable

        async def delayed_confirm(*args, **kwargs):
            result = await confirm(*args, **kwargs)
            admission.clock[0] += timedelta(seconds=2)
            return result

        monkeypatch.setattr(provider_quotes, "confirm_quote_spendable", delayed_confirm)
    elif delay_at == "thread_pool_queue":
        in_pool = test_run_admission.run_in_threadpool

        async def delayed_queue(func, *args, **kwargs):
            if func.__name__ == "consume_intent":
                admission.clock[0] += timedelta(seconds=2)
            return await in_pool(func, *args, **kwargs)

        monkeypatch.setattr(test_run_admission, "run_in_threadpool", delayed_queue)
    elif delay_at == "cas_response":
        transition = admission.store.transition

        def delayed_transition(*args, **kwargs):
            result = transition(*args, **kwargs)
            if kwargs["status"] == "dispatch_started":
                admission.clock[0] += timedelta(seconds=2)
            return result

        monkeypatch.setattr(admission.store, "transition", delayed_transition)
    before = await admission.redis.hgetall(hierarchy.key())
    if delay_at == "none":
        assert (await begin(admission, prepared)).status == "dispatch_started"
    else:
        with pytest.raises(AdmissionRefusedError, match="test_reservation_unconfirmed"):
            await begin(admission, prepared)
    assert await admission.redis.hgetall(hierarchy.key()) == before
    row = admission.store.read_operation(admission.identity.tenant_id, admission.identity.run_id, prepared.receipt.operation_id)
    assert row["status"] == ("dispatch_started" if delay_at in {"none", "cas_response"} else "admitted")
