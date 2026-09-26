"""Retain bounded batch text results before the paid status call can succeed."""

import hashlib
import json
import re
import unicodedata
from urllib.parse import quote

from harness_jobs.identity import OperationRefused
from harness_jobs.leases import lock_lease


def result_text(message):
    """An explicit versioned document, never an arbitrary URL or log fallback."""
    if not message:
        return None
    if not isinstance(message, str) or len(message.encode()) >= 4096:
        raise OperationRefused("batch result is oversized or potentially truncated")
    try:
        document = json.loads(message)
        if (
            set(document) != {"superplane_result_version", "text"}
            or type(document["superplane_result_version"]) is not int
            or document["superplane_result_version"] != 1
            or not isinstance(document["text"], str)
            or "\x00" in document["text"]
        ):
            raise ValueError("invalid result document")
        text = document["text"]
    except (ValueError, TypeError, KeyError):
        raise OperationRefused("batch result document is invalid") from None
    # Same best-effort presentation rule as log windows. Images must not publish
    # credentials; redaction cannot classify arbitrary secrets in user output.
    redacted = re.sub(r"(?i)\bBearer[ \t]+[^\s,}\"']+", "Bearer [REDACTED]", text)
    redacted = re.sub(
        r"-----BEGIN [^-\n]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-\n]*PRIVATE KEY-----|$)",
        "[REDACTED]",
        redacted,
    )
    redacted = re.sub(
        r"""(?i)(["']?(?:token|password|secret|api[_-]?key|access[_-]?token|auth[_-]?token|authorization)["']?\s*[:=]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,}]+)""",
        r"\1[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[REDACTED]", redacted)
    redacted = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED]", redacted
    )
    redacted = "".join(
        char
        for char in redacted
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    return redacted, redacted != text


async def capture(provider, operation, target, plan, known_references, authorize):
    """Only the trusted original provision operation can publish a result.

    No message means no published artifact. A malformed or unverifiable result
    refuses successful settlement; a later recovery must inspect the same work.
    Neither a UI GET nor cleanup invents a result that the executor never captured.
    """
    workspace = provider.workspace
    workspace.require_dedicated_node_authority(target)
    spec = plan.data["workload"]
    from .network_probe_contract import for_operation, verify_result

    probe = for_operation(operation, plan)
    lease = operation.grant.lease
    job_path = workspace.path(target, "Job", spec["name"])
    await authorize()

    if plan.node_bootstrap is not None:
        from .node_command_inventory import require_completed

        await require_completed(provider, operation, plan, authorize)

    async def get(path):
        response = await workspace.request(operation, target, "GET", path)
        if response.status_code != 200:
            raise OperationRefused("batch result source unavailable")
        return response.json()

    job = await get(job_path)
    meta = job.get("metadata", {})
    prefix = f"kubernetes:Job:{target['namespace']}:{spec['name']}:"
    uid = meta.get("uid")
    if (
        not isinstance(uid, str)
        or not uid
        or ":" in uid
        or {ref for ref in known_references if ref.startswith(prefix)} != {prefix + uid}
        or meta.get("namespace") != target["namespace"]
        or meta.get("name") != spec["name"]
        or meta.get("deletionTimestamp") is not None
        or meta.get("annotations", {}).get("superplane.io/approved-request")
        != operation.plan_digest
        or job.get("status", {}).get("succeeded") != 1
    ):
        raise OperationRefused("original completed Job result unavailable")
    containers = (
        job.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    )
    if len(containers) != 1 or any(
        containers[0].get(key, [] if key == "args" else None) != value
        for key, value in {
            "name": "workload",
            "image": spec["image"],
            "command": spec["command"],
            "args": spec["args"],
        }.items()
    ):
        raise OperationRefused("approved Job result invocation changed")
    selector = quote("batch.kubernetes.io/controller-uid=" + uid, safe="")
    listing = await get(
        workspace.path(target, "Pod") + "?limit=32&labelSelector=" + selector
    )
    items = listing.get("items")
    if (
        not isinstance(items, list)
        or len(items) > 32
        or listing.get("metadata", {}).get("continue")
    ):
        raise OperationRefused("batch result Pod listing is incomplete")
    completed = []
    for pod in items:
        metadata = pod.get("metadata", {})
        owners = metadata.get("ownerReferences", [])
        # Selector labels are only an optimization. Foreign Pods are never read.
        if not any(
            owner.get("uid") == uid
            and owner.get("kind") == "Job"
            and owner.get("apiVersion") == "batch/v1"
            and owner.get("name") == spec["name"]
            and owner.get("controller") is True
            for owner in owners
        ):
            continue
        if pod.get("status", {}).get("phase") != "Succeeded":
            continue
        containers = pod.get("spec", {}).get("containers", [])
        statuses = pod.get("status", {}).get("containerStatuses", [])
        if (
            metadata.get("namespace") != target["namespace"]
            or not re.fullmatch(r"[a-zA-Z0-9-]{1,255}", metadata.get("uid", ""))
            or not re.fullmatch(r"[a-z0-9.-]{1,253}", metadata.get("name", ""))
            or len(containers) != 1
            or containers[0].get("name") != "workload"
            or containers[0].get("image") != spec["image"]
            or containers[0].get("command") != spec["command"]
            or containers[0].get("args", []) != spec["args"]
            or len(statuses) != 1
            or statuses[0].get("name") != "workload"
            or statuses[0].get("state", {}).get("terminated", {}).get("exitCode") != 0
        ):
            raise OperationRefused("batch result Pod identity or invocation changed")
        completed.append(pod)
    if len(completed) > 1:
        raise OperationRefused("batch result Pod identity is ambiguous")
    if not completed:
        if probe is not None:
            raise OperationRefused("approved probe Pod evidence unavailable")
        return  # No retained output is claimed, including after external Pod GC.
    pod = completed[0]
    metadata = pod["metadata"]
    placement = await provider.verify_pod_allocation(
        operation, target, plan, pod, authorize
    )
    message = pod["status"]["containerStatuses"][0]["state"]["terminated"].get(
        "message", ""
    )
    content = result_text(message)
    if content is None:
        if probe is not None:
            raise OperationRefused("approved probe result unavailable")
        return
    if probe is not None:
        await verify_result(
            workspace, operation, target, plan, probe, pod, content[0], authorize
        )
    fresh_pod = await get(workspace.path(target, "Pod", metadata["name"]))
    fresh_job = await get(job_path)
    # Exact read documents, not just names, bind the output to one completed Pod.
    if fresh_pod != pod or fresh_job != job:
        raise OperationRefused("batch result source changed while reading")
    if (
        await provider.verify_pod_allocation(operation, target, plan, pod, authorize)
        != placement
    ):
        raise OperationRefused("batch result placement changed during observation")
    if plan.node_bootstrap is not None:
        await require_completed(provider, operation, plan, authorize)
    await authorize()
    text, redacted = content
    digest = hashlib.sha256(text.encode()).hexdigest()
    values = (
        lease.operation_id,
        lease.org_id,
        lease.workspace_id,
        operation.request.parameters["controller_deployment_id"],
        operation.request.parameters["allocation_id"],
        operation.plan_digest,
        uid,
        metadata["uid"],
        text,
        digest,
        redacted,
    )
    # Lock the still-current shared lease across the domain commit. Replays may
    # acknowledge identical content but cannot overwrite an earlier result.
    async with provider.execution_pool.acquire() as execution, execution.transaction():
        if not await lock_lease(execution, lease):
            raise OperationRefused("batch result publication authority expired")
        async with (
            provider.domain_pool.acquire() as connection,
            connection.transaction(),
        ):
            await connection.execute(
                "INSERT INTO controller_batch_results "
                "(operation_id,org_id,workspace_id,deployment_id,allocation_id,plan_digest,job_uid,pod_uid,content,sha256,redacted) "
                "VALUES ($1,$2::text::uuid,$3::text::uuid,$4::text::uuid,$5,$6,$7,$8,$9,$10,$11) "
                "ON CONFLICT(operation_id) DO NOTHING",
                *values,
            )
            row = await connection.fetchrow(
                "SELECT operation_id,org_id::text,workspace_id::text,deployment_id::text,allocation_id,"
                "plan_digest,job_uid,pod_uid,content,sha256,redacted FROM controller_batch_results WHERE operation_id=$1",
                lease.operation_id,
            )
            if row is None or tuple(row.values()) != values:
                raise OperationRefused("retained batch result identity changed")
