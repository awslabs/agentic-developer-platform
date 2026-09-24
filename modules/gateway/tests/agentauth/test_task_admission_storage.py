"""Cross-story admission extensions: budget binding and atomic rate limit."""
# ruff: noqa: F811
import pytest

from src.tasks.store import TaskStoreError, WorkBindingError
from tests.tasks.test_store import AUTHORITY_TABLE, NOW, _request, client, store  # noqa: F401


def test_budget_reservation_is_inside_protected_grant_digest(client, store):
    request = _request(budget_reservation={"reservation_id": "fixture-reservation", "status": "reserved", "amount_usd": "1"})
    store.accept(request)
    assert store.read_task(request.task_id)["budget_reservation"]["reservation_id"] == "fixture-reservation"
    client.update_item(TableName=AUTHORITY_TABLE,
        Key={"pk": {"S": "TENANT#" + request.tenant}, "sk": {"S": request.grant_reference}},
        UpdateExpression="REMOVE budget_reservation")
    with pytest.raises(WorkBindingError):
        store.resolve_work(request.dispatch_id)


def test_admission_rate_count_is_atomic_and_idempotent(client, store):
    scope = "b" * 64
    for index in range(10):
        request = _request(idempotency_key=f"rate-{index}", submit_rate_scope_hash=scope,
                           submit_rate_window_end=int(NOW.timestamp()) + 120)
        store.accept(request)
        assert store.accept(request).replayed
    denied = _request(idempotency_key="eleventh", submit_rate_scope_hash=scope,
                      submit_rate_window_end=int(NOW.timestamp()) + 120)
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

    policies = TaskServicePolicyStore(table_name=AUTHORITY_TABLE, client=client, clock=lambda: NOW)
    client.delete_item(TableName=AUTHORITY_TABLE, Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "TASK_POLICY#svc-principal-1"}})
    policies.put(tenant_id="tenant-a", canonical_principal_id="svc-principal-1", expected_version=0, updated_by="test",
        policy={"status": "active", "allowed_personas": ["agent-task-investigator"], "task_scopes": ["submit"],
            "model_policy_version": "1", "limits": {"max_duration_minutes": 30, "max_turns": 8,
                "max_output_tokens_per_turn": 4096, "max_usd_per_task": 1}})
    reservations = ReservationStore(redis_url=None, ttl_seconds=86400, clock=lambda: NOW.timestamp(),
                                   client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    budget = TaskBudget(BootstrapStore(table_name=AUTHORITY_TABLE, dynamodb_client=client), reservations=reservations,
                        qualification_id="test-qualification", clock=lambda: NOW)
    calls = []

    async def model(*args, **kwargs):
        calls.append(kwargs)
        return _request().model_binding

    service = TaskAdmission(store, policies=policies, budget=budget, model_resolver=model, clock=lambda: NOW)
    caller = SimpleNamespace(tenant_id="tenant-a", principal_id="svc-principal-1", require=lambda scope: None)
    submit = {"schema_version": "1.0", "persona": "agent-task-investigator", "instructions": "investigate"}
    first = await service.admit(caller=caller, submit=submit, idempotency_key="admission-test", db=None)
    replay = await service.admit(caller=caller, submit=submit, idempotency_key="admission-test", db=None)
    assert first["task_id"] == replay["task_id"]
    assert replay["idempotent_replay"]
    assert len(calls) == 1
    task = store.read_task(first["task_id"])
    receipt = task["budget_reservation"]
    await budget.abort_admission(receipt)  # Commit won; an old owner cannot release.
    target = budget._target(scope="qualification:test-qualification", cap=25)
    assert (await reservations.snapshot(target)).total_usd == 1
    await reservations.close()
