"""Cross-story admission extensions: budget binding and atomic rate limit."""

# ruff: noqa: F811
import pytest

from src.tasks.store import TaskStoreError, WorkBindingError
from tests.tasks.test_store import AUTHORITY_TABLE, NOW, _request, client, store  # noqa: F401


def test_budget_reservation_is_inside_protected_grant_digest(client, store):
    request = _request(budget_reservation={"reservation_id": "fixture-reservation", "status": "reserved", "amount_usd": "1"})
    store.accept(request)
    assert store.read_task(request.task_id)["budget_reservation"]["reservation_id"] == "fixture-reservation"
    client.update_item(
        TableName=AUTHORITY_TABLE,
        Key={"pk": {"S": "TENANT#" + request.tenant}, "sk": {"S": request.grant_reference}},
        UpdateExpression="REMOVE budget_reservation",
    )
    with pytest.raises(WorkBindingError):
        store.resolve_work(request.dispatch_id)


def test_admission_rate_count_is_atomic_and_idempotent(client, store):
    scope = "b" * 64
    for index in range(10):
        request = _request(idempotency_key=f"rate-{index}", submit_rate_scope_hash=scope, submit_rate_window_end=int(NOW.timestamp()) + 120)
        store.accept(request)
        assert store.accept(request).replayed
    denied = _request(idempotency_key="eleventh", submit_rate_scope_hash=scope, submit_rate_window_end=int(NOW.timestamp()) + 120)
    with pytest.raises(TaskStoreError):
        store.accept(denied)
    assert store.read_task(denied.task_id) is None
    item = client.get_item(TableName=AUTHORITY_TABLE, Key={"pk": {"S": "TASK_CAPACITY#" + scope}, "sk": {"S": "ACTIVE"}})["Item"]
    assert item["request_count"] == {"N": "10"}


@pytest.mark.asyncio
async def test_real_admission_reserves_once_and_failed_loser_cannot_release(client, store):
    from types import SimpleNamespace

    import fakeredis.aioredis

    from src.agentauth.bootstrap import BootstrapStore
    from src.agentauth.task_admission import TaskAdmission
    from src.agentauth.task_budget import TaskBudget
    from src.agentauth.task_service_policy import TaskServicePolicyStore
    from src.budget.reservations import ReservationStore
    from tests.agentauth.test_task_repository_policy import BINDING

    policies = TaskServicePolicyStore(table_name=AUTHORITY_TABLE, client=client, clock=lambda: NOW)
    client.delete_item(TableName=AUTHORITY_TABLE, Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "TASK_POLICY#svc-principal-1"}})
    policies.put(
        tenant_id="tenant-a",
        canonical_principal_id="svc-principal-1",
        expected_version=0,
        updated_by="test",
        policy={
            "status": "active",
            "repositories": {"application": BINDING},
            "allowed_personas": ["agent-task-investigator"],
            "task_scopes": ["submit"],
            "model_policy_version": "1",
            "limits": {"max_duration_minutes": 30, "max_turns": 8, "max_output_tokens_per_turn": 4096, "max_usd_per_task": 1},
        },
    )
    reservations = ReservationStore(
        redis_url=None, ttl_seconds=86400, clock=lambda: NOW.timestamp(), client=fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    budget = TaskBudget(
        BootstrapStore(table_name=AUTHORITY_TABLE, dynamodb_client=client),
        reservations=reservations,
        qualification_id="test-qualification",
        clock=lambda: NOW,
    )
    calls = []

    async def model(*args, **kwargs):
        calls.append(kwargs)
        return _request().model_binding

    service = TaskAdmission(store, policies=policies, budget=budget, model_resolver=model, clock=lambda: NOW)
    caller = SimpleNamespace(tenant_id="tenant-a", principal_id="svc-principal-1", require=lambda scope: None)
    submit = {
        "schema_version": "1.0",
        "persona": "agent-task-investigator",
        "instructions": "investigate",
        "inputs": {"repository_binding": "application"},
    }
    first = await service.admit(caller=caller, submit=submit, idempotency_key="admission-test", db=None)
    replay = await service.admit(caller=caller, submit=submit, idempotency_key="admission-test", db=None)
    assert first["task_id"] == replay["task_id"]
    assert replay["idempotent_replay"]
    task = store.read_task(first["task_id"])
    grant = store._get_authority("TENANT#tenant-a", task["grant_reference"])
    assert grant["repository_binding"] == {"alias": "application", "binding": BINDING}
    assert len(calls) == 1
    task = store.read_task(first["task_id"])
    receipt = task["budget_reservation"]
    await budget.abort_admission(receipt)  # Commit won; an old owner cannot release.
    target = budget._target(scope="qualification:test-qualification", cap=25)
    assert await reservations.snapshot(target) is None
    await reservations.close()


@pytest.mark.asyncio
async def test_task_budget_settlement_verifies_actual_redis_receipt(client):
    from decimal import Decimal
    from unittest.mock import AsyncMock

    import fakeredis.aioredis

    from src.agentauth.bootstrap import BootstrapStore
    from src.agentauth.task_budget import TaskBudget, TaskBudgetError
    from src.budget.reservations import ReservationStore

    reservations = ReservationStore(
        redis_url=None, ttl_seconds=86400, clock=lambda: NOW.timestamp(), client=fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    budget = TaskBudget(
        BootstrapStore(table_name=AUTHORITY_TABLE, dynamodb_client=client),
        reservations=reservations,
        qualification_id="test-qualification",
        clock=lambda: NOW,
    )
    target = await budget.reserve_model(task_id="task", operation_id="op", cap=1, amount="0.2")
    reconcile = reservations.reconcile
    reservations.reconcile = AsyncMock()  # Shared implementation may return after swallowing a backend failure.
    with pytest.raises(TaskBudgetError, match="unconfirmed"):
        await budget.settle_model(operation_id="op", target=target, actual_usd="0.01")
    assert (await reservations.snapshot(target)).total_usd == Decimal("0.2")
    reservations.reconcile = reconcile
    await budget.settle_model(operation_id="op", target=target, actual_usd="0.01")
    assert (await reservations.snapshot(target)).total_usd == Decimal("0.01")
    await reservations.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("committed", [False, True])
async def test_expired_preacceptance_hold_cleanup_is_owner_fenced(client, committed):
    import hashlib
    from datetime import timedelta

    import fakeredis.aioredis

    from src.agentauth.bootstrap import BootstrapStore
    from src.agentauth.task_budget import TaskBudget
    from src.budget.reservations import ReservationStore

    now = [NOW]
    reservations = ReservationStore(
        redis_url=None, ttl_seconds=86400, clock=lambda: now[0].timestamp(), client=fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    budget = TaskBudget(
        BootstrapStore(table_name=AUTHORITY_TABLE, dynamodb_client=client),
        reservations=reservations,
        qualification_id="test-qualification",
        clock=lambda: now[0],
    )
    hold = await budget.reserve_admission(tenant="tenant", principal="principal", idempotency_key="abandoned", max_usd=1, request_digest="a" * 64)
    shard_number = int(hashlib.sha256(hold["reservation_id"].encode()).hexdigest()[:2], 16) % 16
    shard = f"v1#{shard_number:02d}"
    target = budget._target(scope="qualification:test-qualification", cap=25)
    await budget.reap_abandoned(shard=shard)
    assert await reservations.snapshot(target) is None
    if committed:
        client.update_item(
            TableName=AUTHORITY_TABLE,
            Key={"pk": {"S": hold["authority_pk"]}, "sk": {"S": "RESERVATION"}},
            UpdateExpression="SET #state = :committed",
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":committed": {"S": "committed"}},
        )
    now[0] += timedelta(seconds=121)
    await budget.reap_abandoned(shard=shard)
    assert await reservations.snapshot(target) is None
    raw = budget.authority._read(hold["authority_pk"], "RESERVATION")
    assert raw["state"] == {"S": "committed" if committed else "released"}
    assert not client.query(
        TableName=AUTHORITY_TABLE, KeyConditionExpression="pk = :pk", ExpressionAttributeValues={":pk": {"S": "TASK_ADMISSION_CLEANUP#" + shard}}
    ).get("Items")
    await reservations.close()


def test_repository_binding_is_inside_durable_protected_grant(client, store):
    from tests.agentauth.test_task_repository_policy import BINDING

    request = _request(repository_binding={"alias": "application", "binding": BINDING})
    store.accept(request)
    grant = store._get_authority("TENANT#" + request.tenant, request.grant_reference)
    assert grant["repository_binding"] == request.repository_binding
    store.resolve_work(request.dispatch_id)
    client.update_item(
        TableName=AUTHORITY_TABLE,
        Key={"pk": {"S": "TENANT#" + request.tenant}, "sk": {"S": request.grant_reference}},
        UpdateExpression="REMOVE repository_binding",
    )
    with pytest.raises(WorkBindingError):
        store.resolve_work(request.dispatch_id)


@pytest.mark.asyncio
async def test_admission_above_old_pilot_caps_needs_no_qualification_or_redis(client, monkeypatch):
    from unittest.mock import AsyncMock

    from src.agentauth.bootstrap import BootstrapStore
    from src.agentauth.task_budget import TaskBudget

    monkeypatch.delenv("ADP_TASK_QUALIFICATION_ID", raising=False)
    ledger = AsyncMock()
    budget = TaskBudget(BootstrapStore(table_name=AUTHORITY_TABLE, dynamodb_client=client), reservations=ledger, clock=lambda: NOW)
    receipt = await budget.reserve_admission(tenant="tenant", principal="principal", idempotency_key="large", max_usd=100, request_digest="a" * 64)
    assert receipt["amount_usd"] == "100"
    assert receipt["targets"] == []
    await budget.settle_admission(receipt, actual_usd=50)
    await budget.abort_admission(receipt)
    ledger.reserve.assert_not_awaited()
    ledger.reconcile.assert_not_awaited()
    ledger._get_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_pilot_hold_is_released_before_retry_and_task_cap_still_enforced(client):
    from dataclasses import asdict
    from datetime import timedelta
    from decimal import Decimal

    import fakeredis.aioredis
    from boto3.dynamodb.types import TypeSerializer

    from src.agentauth.bootstrap import BootstrapStore
    from src.agentauth.task_budget import TaskBudget, TaskBudgetError
    from src.budget.reservations import ReservationStore

    now = [NOW]
    ledger = ReservationStore(
        redis_url=None, ttl_seconds=86400, clock=lambda: now[0].timestamp(), client=fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    budget = TaskBudget(BootstrapStore(table_name=AUTHORITY_TABLE, dynamodb_client=client), reservations=ledger, clock=lambda: now[0])
    args = dict(tenant="tenant", principal="principal", idempotency_key="legacy", max_usd=1, request_digest="b" * 64)
    old = await budget.reserve_admission(**args)
    target = budget._target(scope="qualification:retired", cap=25)
    await budget._initialize(target)
    assert (await ledger.reserve(old["reservation_id"], Decimal(1), [target])).admitted
    targets = [{**asdict(target), "headroom_usd": "25"}]
    client.update_item(
        TableName=AUTHORITY_TABLE,
        Key={"pk": {"S": old["authority_pk"]}, "sk": {"S": "RESERVATION"}},
        UpdateExpression="SET targets = :targets, qualification_id = :old",
        ExpressionAttributeValues={":targets": TypeSerializer().serialize(targets), ":old": {"S": "retired"}},
    )
    now[0] += timedelta(seconds=121)
    new = await budget.reserve_admission(**{**args, "max_usd": 100})
    assert new["targets"] == []
    assert new["owner_token"] != old["owner_token"]
    assert (await ledger.snapshot(target)).total_usd == 0
    await budget.reserve_model(task_id="task", operation_id="first", cap=1, amount="0.8")
    with pytest.raises(TaskBudgetError, match="reservation refused"):
        await budget.reserve_model(task_id="task", operation_id="second", cap=1, amount="0.3")
    await ledger.close()
