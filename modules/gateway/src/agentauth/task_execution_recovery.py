"""Bounded, server-observed recovery of a stopped Task with no model claims."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from starlette.concurrency import run_in_threadpool

from src.agentauth.task_budget_settlement import settle_task_admission
from src.agentauth.task_runtime import TaskRuntime, VerifiedTaskAttempt
from src.tasks.records import task_ops_partition
from src.tasks.store import TaskStoreError, _serialize
from src.tasks.task_commands import TaskCommands


def terminated_workload(verifier, *, uid, namespace, name=None):
    """Missing/deleted pods are unknown; only the exact live API object proves stop."""
    retention = verifier.exit_retention
    if namespace != retention.namespace:
        return None
    if name:
        _, pod, _ = retention._read(name, uid)
        pods = [pod]
    else:
        response = retention.client.get(f"/api/v1/namespaces/{namespace}/pods", headers=retention._headers(), params={"limit": "100"})
        response.raise_for_status()
        pods = response.json().get("items", [])
    for pod in pods:
        if pod.get("metadata", {}).get("uid") != uid:
            continue
        if pod.get("spec", {}).get("serviceAccountName") != retention.service_account or pod.get("spec", {}).get("restartPolicy") != "Never":
            return None
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"} or not statuses:
            return None
        exits = [status.get("state", {}).get("terminated") for status in statuses]
        if any(not item or not item.get("finishedAt") for item in exits):
            return None
        return max(item["finishedAt"] for item in exits)
    return None


def finalize_no_send(repository, identity, *, observed_at):
    snapshot = repository.read_task(identity.task_id)
    if snapshot is None:
        raise TaskStoreError("task unavailable")
    # Model claims advance META version in the same transaction that creates the
    # claim. The finalize CAS below therefore closes the read/claim race.
    page = repository._client.query(
        TableName=repository.table_name,
        KeyConditionExpression="event_id = :pk AND begins_with(arrived_at, :model)",
        ExpressionAttributeValues=_serialize({":pk": task_ops_partition(identity.task_id), ":model": "MODEL#"}),
        ConsistentRead=True,
        Limit=1,
    )
    if page.get("Items") or page.get("LastEvaluatedKey"):
        return False  # Provider uncertainty is retained for separate reconciliation.
    if snapshot["state"] in {"completed", "failed", "cancelled"}:
        return True
    outcome = "cancelled" if snapshot["state"] == "cancel_requested" else "failed"
    TaskCommands(repository).finalize(
        identity,
        {
            "schema_version": "1.0",
            "outcome": outcome,
            "final_report_id": str(uuid.uuid4()),
            "child_exit": {"confirmed": False, "exit_code": None, "signal": None, "stopped_at": None},
            "result": None,
            "committed_result_refs": [],
            "error": {
                "schema_version": "1.0",
                "outcome": outcome,
                "code": "cancelled_by_client" if outcome == "cancelled" else "process_failed",
                "message": "The assigned workload terminated before any model request was claimed.",
                "committed_at": observed_at,
                "child_exit_confirmed": False,
                "recovery_required": True,
                "provider_outcome": "not_started",
                "total_usd": 0,
            },
        },
        stop_only=True,
        expected_version=int(snapshot["version"]),
    )
    return True


async def recover_execution(repository, runtime, *, work_id, lease_token):
    work = await run_in_threadpool(repository.resolve_work, work_id)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if work.get("work_kind") != "execution" or work.get("recovery_lease_token") != lease_token or work.get("recovery_lease_expires_at", "") < now:
        raise TaskStoreError("execution recovery lease unavailable")
    task = await run_in_threadpool(repository.read_task, work["task_id"])
    service = TaskRuntime(repository)
    grant = await run_in_threadpool(service._grant, task["scope"]["tenant"], task["invocation_id"], int(task["generation"]), stop_only=True)
    uid, namespace = grant.get("workload_uid"), grant.get("workload_namespace")
    if not uid or not namespace:
        return "unknown", task["state"]
    attempt = task.get("runtime_attempt_id")
    if not attempt or grant.get("runtime_attempt_id") != attempt:
        return "unknown", task["state"]
    identity = VerifiedTaskAttempt(
        task["task_id"], task["invocation_id"], int(task["generation"]), attempt, task["scope"]["tenant"], task["scope"]["canonical_principal"], uid
    )
    if (identity.task_id, identity.invocation_id, identity.generation) != (work["task_id"], work["invocation_id"], int(work["generation"])):
        raise TaskStoreError("execution recovery identity changed")
    stopped = task.get("child_exit", {}).get("confirmed") or (
        task.get("server_workload_terminated") and task.get("stop_evidence", {}).get("workload_terminated")
    )
    name = grant.get("workload_name")
    if not stopped:
        observed = await run_in_threadpool(terminated_workload, runtime.workloads, uid=uid, namespace=namespace, name=name)
        if not observed:
            return "unknown", task["state"]
        # No-send work gets a proven zero-cost failure; model claims remain
        # untouched and native settlement records provider uncertainty instead.
        await run_in_threadpool(finalize_no_send, repository, identity, observed_at=observed)
        await run_in_threadpool(
            TaskCommands(repository).settlement,
            identity,
            {"stop_evidence": {"child_exit_confirmed": False, "workload_terminated": True, "observed_at": observed}, "queue_ack_status": "pending"},
            verified_workload=True,
        )
    # Durable process/observed workload evidence survives pod garbage collection.
    # Financial uncertainty must never hold a Kubernetes finalizer indefinitely.
    if name:
        await run_in_threadpool(
            release_retention, runtime.workloads, name=name, uid=uid, invocation_id=identity.invocation_id, tenant_id=identity.tenant
        )
    if not await settle_task_admission(repository, identity):
        return "unknown", (await run_in_threadpool(repository.read_task, identity.task_id))["state"]
    if (await run_in_threadpool(repository.read_task, identity.task_id)).get("queue_ack_status") != "confirmed":
        return "unknown", (await run_in_threadpool(repository.read_task, identity.task_id))["state"]
    # Retire this one discovery item only under the original recovery lease.
    await run_in_threadpool(
        repository._client.update_item,
        TableName=repository.table_name,
        Key=_serialize({"event_id": work["event_id"], "arrived_at": work["arrived_at"]}),
        UpdateExpression="SET recovery_state = :settled REMOVE task_work_shard, task_due, recovery_lease_token, recovery_lease_expires_at",
        ConditionExpression="work_id = :id AND recovery_lease_token = :token",
        ExpressionAttributeValues=_serialize({":settled": "settled", ":id": work_id, ":token": lease_token}),
    )
    return "confirmed", (await run_in_threadpool(repository.read_task, identity.task_id))["state"]


def release_retention(verifier, *, name, uid, invocation_id, tenant_id):
    import httpx

    from src.agentauth.exit_retention import ExitRetentionError

    try:
        verifier.exit_retention.release(name=name, uid=uid, invocation_id=invocation_id, tenant_id=tenant_id)
    except ExitRetentionError as exc:
        cause = exc.__cause__
        if isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 404:
            return  # The task already retains stop proof; this is cleanup only.
        raise
