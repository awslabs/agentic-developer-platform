"""Release admission headroom only from terminal, stopped, durably priced work."""
from decimal import Decimal

from starlette.concurrency import run_in_threadpool

from src.agentauth.task_budget import TaskBudgetError, task_budget
from src.tasks.records import task_ops_partition
from src.tasks.store import _deserialize, _serialize


async def settle_task_admission(repository, identity, *, budget=None):
    budget = budget or task_budget(repository)
    task = await run_in_threadpool(repository.read_task, identity.task_id)
    if task is None or (task["invocation_id"], int(task["generation"]), task.get("runtime_attempt_id")) != (
            identity.invocation_id, identity.generation, identity.runtime_attempt_id):
        raise TaskBudgetError("task settlement attempt changed")
    receipt = task.get("budget_reservation")
    if not receipt or task["state"] not in {"completed", "failed", "cancelled"}:
        return False
    stop = task.get("stop_evidence") or {}
    if not (task.get("runtime_not_started") is True or task.get("child_exit", {}).get("confirmed")
            or stop.get("child_exit_confirmed") or stop.get("workload_terminated")):
        return False
    page = await run_in_threadpool(repository._client.query, TableName=repository.table_name,
        KeyConditionExpression="event_id = :pk AND begins_with(arrived_at, :model)",
        ExpressionAttributeValues=_serialize({":pk": task_ops_partition(identity.task_id), ":model": "MODEL#"}),
        ConsistentRead=True, Limit=9)
    rows = [_deserialize(row) for row in page.get("Items", [])]
    if page.get("LastEvaluatedKey") or len(rows) > 8:
        return False
    amount = Decimal(0)
    for row in rows:
        if row.get("operation_status") == "rejected" and row.get("handoff") == "not_started":
            continue  # Provider was provably never invoked by this operation.
        if (row.get("operation_status") != "confirmed" or row.get("pricing_decision", {}).get("confidence") != "verified"
                or row.get("usage_logged") is not True or not row.get("provider_request_id")):
            return False
        amount += Decimal(row["pricing_decision"]["ledger_cost_usd"])
    await budget.settle_admission(receipt, actual_usd=amount)
    return True
