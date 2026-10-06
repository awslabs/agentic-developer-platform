"""Non-consuming proof of a configured producer's installed native paid worker."""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.auth.agent_registry import get_agent_registry_service
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.domain_operation_routes import DomainOperationRoute, DomainScope, producer
from src.internal.domain_operation_runtime import EXECUTOR_SCOPE, RECOVERY_SCOPE, bootstrap_store, runtime_for
from src.internal.domain_operation_store import aws_client, domain_connect, operation_connect

router = APIRouter(
    route_class=DomainOperationRoute,
    prefix="/internal/v1/controller-execution",
    tags=["domain-operations"],
    dependencies=[Depends(verify_internal_or_irsa)],
)
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")
_QUEUE = re.compile(r"https://sqs\.([a-z0-9-]+)\.amazonaws\.com/(\d{12})/([A-Za-z0-9_-]+)")


class BindingProofRequest(DomainScope):
    state: Literal["prepared", "executable", "quiescent"] = "executable"


def installed_worker(binding, runtime, state="executable"):
    """Read the installed ScaledJob; a zero-replica installation has no pod to review."""
    namespace, name = binding.worker_namespace, binding.worker_scaled_job
    if any(not _NAME.fullmatch(value) for value in (namespace, name, binding.worker_service_account)):
        raise HTTPException(503, "paid worker binding unavailable")
    verifier = runtime.workloads
    try:
        token = verifier._gateway_token_path.read_text().strip()
        if not token:
            raise ValueError("missing gateway Kubernetes token")
        headers = {"Authorization": f"Bearer {token}"}

        def read(path):
            result = verifier._client.get(path, headers=headers)
            result.raise_for_status()
            return result.json()

        base = f"/api/v1/namespaces/{namespace}"
        job = read(f"/apis/keda.sh/v1alpha1/namespaces/{namespace}/scaledjobs/{name}")
        service_account = read(f"{base}/serviceaccounts/{binding.worker_service_account}")
        configuration = read(f"{base}/configmaps/{name}-config")
    except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "paid worker installation unavailable") from None
    try:
        metadata = job["metadata"]
        spec = job["spec"]
        pod = spec["jobTargetRef"]["template"]["spec"]
        workers = [container for container in pod["containers"] if container["name"] == binding.worker_container]
        (worker,) = workers
        images = worker["image"].rsplit("@", 1)
        digest = images[1]
        environment = {entry["name"]: entry.get("value") for entry in worker["env"]}
        sources = worker["envFrom"]
        (source,) = sources
        triggers = spec["triggers"]
        (trigger,) = triggers
        account = service_account["metadata"]
        role = account["annotations"]["eks.amazonaws.com/role-arn"]
        queue = _QUEUE.fullmatch(binding.queue_url)
        # Native lifecycle uses the paid provider broker. Workload/SkyPilot
        # Secrets and a controller sidecar must not gate first workspace create.
        volumes = pod.get("volumes", [])
        secret_volumes = [volume for volume in volumes if "secret" in volume]
        database_only = (
            len(secret_volumes) == 1
            and secret_volumes[0].get("name") == "database"
            and secret_volumes[0]["secret"].get("items") == [{"key": key, "path": key} for key in ("domain-dsn", "execution-dsn", "ca.pem")]
        )
        if (
            metadata["namespace"] != namespace
            or metadata["name"] != name
            or metadata.get("deletionTimestamp")
            or state not in {"prepared", "executable"}
            or type(spec["maxReplicaCount"]) is not int
            or (state == "prepared" and (metadata.get("annotations", {}).get("autoscaling.keda.sh/paused") != "true" or spec["maxReplicaCount"] != 0))
            or (
                state == "executable"
                and (metadata.get("annotations", {}).get("autoscaling.keda.sh/paused") == "true" or not 1 <= spec["maxReplicaCount"] <= 4)
            )
            or not images[0]
            or digest not in binding.worker_image_digests
            or pod["serviceAccountName"] != binding.worker_service_account
            or worker.get("command")
            or worker.get("args")
            or environment.get("ADP_AGENT_AUTHORITY_ENABLED") != "true"
            or environment.get("SUPERPLANE_PAID_WORKER_MODE") != "native-lifecycle"
            or len(pod["containers"]) != 1
            or pod.get("initContainers")
            or not database_only
            or any(value.get("name") in {"workspaces", "provider"} for value in volumes)
            or any("secret" in source for volume in volumes for source in volume.get("projected", {}).get("sources", []))
            or {"SUPERPLANE_WORKSPACE_CREDENTIALS_DIR", "SKYPILOT_SERVICE_TOKEN_FILE"}.intersection(environment)
            or source != {"configMapRef": {"name": name + "-config"}}
            or configuration["metadata"]["namespace"] != namespace
            or configuration["metadata"]["name"] != name + "-config"
            or configuration["metadata"].get("deletionTimestamp")
            or configuration["data"]["SUPERPLANE_OPERATION_SCHEMA"] != binding.database_schema
            or configuration["data"]["SUPERPLANE_DOMAIN_SCHEMA"] != binding.domain_database_schema
            or account["namespace"] != namespace
            or account["name"] != binding.worker_service_account
            or account.get("deletionTimestamp")
            or not queue
            or not role.startswith(f"arn:aws:iam::{queue[2]}:role/")
            or trigger["metadata"]["queueURL"] != binding.queue_url
            or trigger["metadata"]["awsRegion"] != queue[1]
        ):
            raise ValueError("installed paid worker disagrees with binding")
        return digest, role, f"arn:aws:sqs:{queue[1]}:{queue[2]}:{queue[3]}"
    except (KeyError, IndexError, TypeError, ValueError):
        raise HTTPException(503, "paid worker installation differs from binding") from None


@router.post("/binding-proof")
async def binding_proof(body: BindingProofRequest, request: Request):
    binding = producer(request, body)
    async with domain_connect(binding) as connection:
        mapped = await connection.fetchval("SELECT adp_org_id FROM organizations WHERE id::text=$1", binding.org_id)
    if mapped != binding.adp_org_id:
        raise HTTPException(503, "domain tenant mapping unavailable")
    state = getattr(body, "state", "executable")
    quiescent = None
    if state in {"prepared", "quiescent"}:
        async with operation_connect(binding) as connection:
            quiescent = not await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM harness_operations WHERE org_id=$1 "
                "AND (state IN ('pending','running') OR cleanup_required)) "
                "OR EXISTS (SELECT 1 FROM harness_operation_leases WHERE org_id=$1 AND closed_at IS NULL) "
                "OR EXISTS (SELECT 1 FROM harness_dispatch_outbox WHERE org_id=$1 "
                "AND delivered_at IS NULL AND abandoned_at IS NULL)",
                binding.org_id,
            )
        if not quiescent:
            raise HTTPException(409, "paid worker preparation has outstanding operations")
    if state == "quiescent":
        if producer(request, body) != binding:
            raise HTTPException(403, "paid domain producer changed")
        return {
            "version": 1,
            "checked_at": datetime.now(UTC).isoformat(),
            "installed": False,
            "state": state,
            "quiescent": True,
            "domain": binding.domain,
            "org_id": binding.org_id,
            "adp_org_id": binding.adp_org_id,
        }
    store = bootstrap_store()
    table = await run_in_threadpool(store.client.describe_table, TableName=store.table)
    if table.get("Table", {}).get("TableStatus") != "ACTIVE":
        raise HTTPException(503, "domain worker authority unavailable")
    digest, role, queue_arn = await run_in_threadpool(installed_worker, binding, runtime_for(binding), state)
    agent = await run_in_threadpool(get_agent_registry_service().get_current_agent, binding.worker_registry_id, role)
    if (
        agent is None
        or agent.get("org_id") != binding.adp_org_id
        or agent.get("scope") not in {"internal", "platform-internal"}
        or not {EXECUTOR_SCOPE, RECOVERY_SCOPE}.issubset(agent.get("credential_scopes", []))
    ):
        raise HTTPException(503, "paid worker registration unavailable")
    actual = await run_in_threadpool(
        aws_client("sqs").get_queue_attributes,
        QueueUrl=binding.queue_url,
        AttributeNames=["QueueArn"],
    )
    if actual.get("Attributes", {}).get("QueueArn") != queue_arn:
        raise HTTPException(503, "paid worker queue differs from binding")
    if producer(request, body) != binding:
        raise HTTPException(403, "paid domain producer changed")
    return {
        "version": 1,
        "checked_at": datetime.now(UTC).isoformat(),
        "installed": True,
        "state": state,
        "quiescent": quiescent,
        "domain_schema": binding.domain_database_schema,
        "binding_sha256": hashlib.sha256(json.dumps(asdict(binding), sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "domain": binding.domain,
        "org_id": binding.org_id,
        "adp_org_id": binding.adp_org_id,
        "producer_registry_id": binding.producer_registry_id,
        "worker_registry_id": binding.worker_registry_id,
        "worker_namespace": binding.worker_namespace,
        "worker_service_account": binding.worker_service_account,
        "worker_role_arn": role,
        "worker_image_digest": digest,
        "operation_schema": binding.database_schema,
        "queue_arn": queue_arn,
    }
