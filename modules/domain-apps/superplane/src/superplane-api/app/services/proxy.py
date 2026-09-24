"""Kubernetes proxy service — brokers a workspace role and calls the workspace cluster.

Pattern: look up cluster in DB -> broker the workspace IAM role (boto3 STS, with the
tenant's stored ExternalId) -> build a signed EKS token -> create a TLS-verified K8s
client for the workspace cluster -> forward request -> return response.

Brokered API calls through this service are the default path for workload operations:
the control plane performs the operation for an already-authorized caller and the caller
never receives a credential. Exporting a kubeconfig (``app.services.kubeconfig``) is the
exceptional path.

Authentication and TLS live in ``app.services.eks_auth``, which is the only supported
source of cluster bearer tokens and CA bundles. There is no code path here that disables
TLS verification: a cluster whose CA cannot be resolved is refused, not connected to
insecurely.
"""

import logging
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from kubernetes.client import ApiClient, AppsV1Api, Configuration, CoreV1Api
from kubernetes.client.rest import ApiException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.cloud_account import CloudAccount
from app.models.cluster import Cluster
from app.models.workspace import STATUS_ACTIVE, Workspace
from app.services.deployment_identity import has_create_binding, matches_create
from app.services.eks_auth import (
    EksAuthError,
    build_cluster_token,
    describe_cluster_ca,
    write_ca_bundle,
)

logger = logging.getLogger(__name__)

# STS token duration (15 minutes — minimum)
TOKEN_DURATION_SECONDS = 900
WORKSPACE_OWNER_LABEL = "superplane.io/workspace"


class ProxyError(Exception):
    """Raised when a proxy operation fails."""

    def __init__(self, message: str, status_code: int = 502):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


async def get_workspace_cluster(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
    *,
    for_update: bool = False,
) -> tuple[Workspace, Cluster]:
    """Fetch workspace and its associated cluster, validating ownership.

    Raises:
        ProxyError: If workspace not found, not active, or no cluster attached.
    """
    query = (
        select(Workspace)
        .where(
            Workspace.id == workspace_id,
            Workspace.org_id == org_id,
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    workspace = result.scalar_one_or_none()

    if workspace is None:
        raise ProxyError("Workspace not found", status_code=404)

    if workspace.status not in {STATUS_ACTIVE, "Active"}:
        raise ProxyError(
            f"Workspace is not active (status: {workspace.status}). Proxy requires an active workspace.",
            status_code=400,
        )

    if not workspace.cluster_id:
        raise ProxyError("No cluster associated with this workspace", status_code=400)

    query = (
        select(Cluster)
        .where(
            Cluster.id == workspace.cluster_id,
            Cluster.org_id == org_id,
            or_(Cluster.workspace_id == workspace_id, Cluster.workspace_id.is_(None)),
        )
        .execution_options(populate_existing=True)
    )
    if for_update:
        query = query.with_for_update()
    cluster_result = await db.execute(query)
    cluster = cluster_result.scalar_one_or_none()

    if cluster is None or not cluster.endpoint:
        raise ProxyError("Cluster endpoint not available", status_code=400)

    return workspace, cluster


def _get_aws_account_from_cluster(cluster: Cluster) -> str:
    """Extract AWS account ID from cluster's EKS ARN."""
    if cluster.eks_cluster_arn:
        parts = cluster.eks_cluster_arn.split(":")
        if len(parts) >= 5:
            return parts[4]
    return ""


def _get_region_from_cluster(cluster: Cluster) -> str:
    """Extract the region from the cluster's EKS ARN, falling back to the API's region.

    The workspace cluster is not necessarily in the control plane's own region, and
    signing an EKS token against the wrong regional STS endpoint produces a token the
    cluster rejects. The ARN is authoritative for where the cluster actually is.
    """
    if cluster.eks_cluster_arn:
        parts = cluster.eks_cluster_arn.split(":")
        if len(parts) >= 4 and parts[3]:
            return parts[3]
    return settings.aws_region


def _get_cluster_name_from_arn(cluster: Cluster) -> str:
    """Extract the EKS cluster name from the ARN, falling back to the record's name.

    The name signed into the token must be the cluster's real EKS name
    (``arn:aws:eks:<region>:<account>:cluster/<name>``), which is not necessarily the
    display name stored on the row.
    """
    if cluster.eks_cluster_arn and "/" in cluster.eks_cluster_arn:
        candidate = cluster.eks_cluster_arn.rsplit("/", 1)[-1]
        if candidate:
            return candidate
    return cluster.name or ""


async def _get_workspace_external_id(
    workspace: Workspace,
    db: AsyncSession,
) -> str | None:
    """Read the tenant's ExternalId from their stored cloud-account record.

    Per U16a (#5051) the value is *read from stored metadata, never derived in code*: a
    value computed here would drift from what the tenant put in their role's trust policy
    (so every assume fails) or be guessable (so the confused-deputy protection it exists
    to provide is absent). Returns None when the workspace has no linked account, leaving
    the caller to assume without the condition exactly as before for legacy records.
    """
    if not workspace.aws_account_id:
        return None

    result = await db.execute(
        select(CloudAccount).where(
            CloudAccount.id == workspace.aws_account_id,
            # Scoped to the workspace's own org so one tenant's record can never
            # supply the ExternalId used to broker another tenant's role.
            CloudAccount.org_id == workspace.org_id,
        )
    )
    account = result.scalar_one_or_none()
    return account.external_id if account else None


def assume_role_for_cluster(
    aws_account_id: str,
    workspace_name: str,
    session_suffix: str = "proxy",
    external_id: str | None = None,
) -> dict:
    """Broker the workspace IAM role in the workspace's AWS account.

    Role ARN pattern: arn:aws:iam::{account_id}:role/superplane-workspace-{name}

    Args:
        aws_account_id: Account the workspace cluster lives in.
        workspace_name: Workspace name, used for the role name and session name.
        session_suffix: Distinguishes proxy sessions from kubeconfig sessions in
            CloudTrail.
        external_id: The tenant's stored ExternalId. Sent only when present, so roles
            whose trust policy has no ``sts:ExternalId`` condition keep working; an
            empty value is not equivalent to omitting the parameter.

    Returns:
        dict with AccessKeyId, SecretAccessKey, SessionToken. These are for signing —
        the SessionToken is never used as a Kubernetes bearer token.
    """
    sts_client = boto3.client("sts", region_name=settings.aws_region)
    role_arn = (
        f"arn:aws:iam::{aws_account_id}:role/superplane-workspace-{workspace_name}"
    )

    params: dict[str, Any] = {
        "RoleArn": role_arn,
        "RoleSessionName": f"superplane-{session_suffix}-{workspace_name}",
        "DurationSeconds": TOKEN_DURATION_SECONDS,
    }
    if external_id:
        params["ExternalId"] = external_id

    try:
        response = sts_client.assume_role(**params)
        return response["Credentials"]
    except NoCredentialsError as exc:
        logger.error("AWS credentials not configured for proxy: %s", exc)
        raise ProxyError(
            "AWS credentials not configured. Ensure the API server has IAM role access (IRSA).",
            status_code=503,
        ) from exc
    except ClientError as exc:
        logger.error("Failed to assume role %s: %s", role_arn, exc)
        raise ProxyError(f"Failed to assume cross-account role: {exc}") from exc


def create_k8s_client(
    cluster_endpoint: str,
    cluster_ca_data: str,
    credentials: dict,
    cluster_name: str,
    region: str,
) -> ApiClient:
    """Create a TLS-verified Kubernetes API client authenticated with a signed EKS token.

    Args:
        cluster_endpoint: EKS API server endpoint URL.
        cluster_ca_data: Base64-encoded CA certificate data. Required — there is no
            unverified mode.
        credentials: Brokered STS credentials, used to *sign* the token.
        cluster_name: EKS cluster name, signed into the token to bind it to this cluster.
        region: Region whose STS endpoint the token is signed against.

    Returns:
        Configured kubernetes ApiClient with verification enabled.

    Raises:
        ProxyError: If a signed token cannot be built or the CA cannot be established.
            Both are refusals rather than degraded connections: continuing without a
            verified CA would send brokered credentials to an unauthenticated endpoint,
            and continuing without a valid token would fail at the cluster anyway.
    """
    configuration = Configuration()
    configuration.host = cluster_endpoint

    try:
        # A signed, cluster-bound EKS token — NOT credentials["SessionToken"], which the
        # Kubernetes API server cannot verify and EKS does not accept.
        token = build_cluster_token(
            cluster_name=cluster_name,
            credentials=credentials,
            region=region,
        )
        ca_path = write_ca_bundle(cluster_ca_data)
    except EksAuthError as exc:
        raise ProxyError(str(exc), status_code=502) from exc

    configuration.api_key = {"BearerToken": token}
    configuration.api_key_prefix = {"BearerToken": "Bearer"}

    # Verify against the cluster's own CA. Set explicitly rather than relying on the
    # library default so that the security property is visible at the call site.
    configuration.ssl_ca_cert = ca_path
    configuration.verify_ssl = True

    return ApiClient(configuration)


async def get_k8s_clients(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[CoreV1Api, AppsV1Api, Workspace, Cluster]:
    """Full proxy setup: look up cluster, broker the role, create verified K8s clients.

    This is the brokered default path for workload operations: the caller is already
    authenticated and authorized for ``workspace_id`` (ownership is enforced by
    :func:`get_workspace_cluster`, which scopes the lookup to ``org_id``), the control
    plane performs the operation, and no credential is returned to the caller.

    Returns:
        Tuple of (CoreV1Api, AppsV1Api, workspace, cluster).
    """
    workspace, cluster = await get_workspace_cluster(workspace_id, org_id, db)

    aws_account_id = _get_aws_account_from_cluster(cluster)
    if not aws_account_id:
        raise ProxyError("Cannot determine AWS account for cluster proxy")

    cluster_name = _get_cluster_name_from_arn(cluster)
    if not cluster_name:
        raise ProxyError("Cannot determine EKS cluster name for cluster proxy")

    region = _get_region_from_cluster(cluster)
    external_id = await _get_workspace_external_id(workspace, db)

    credentials = assume_role_for_cluster(
        aws_account_id,
        workspace.name,
        external_id=external_id,
    )

    # EKS is authoritative for the cluster's CA, so resolve it live through the same
    # brokered credentials rather than trusting a cached copy that a cluster CA rotation
    # would silently invalidate.
    try:
        ca_data = describe_cluster_ca(
            cluster_name=cluster_name,
            credentials=credentials,
            region=region,
        )
    except EksAuthError as exc:
        raise ProxyError(str(exc), status_code=502) from exc

    api_client = create_k8s_client(
        cluster_endpoint=cluster.endpoint or "",
        cluster_ca_data=ca_data,
        credentials=credentials,
        cluster_name=cluster_name,
        region=region,
    )

    return CoreV1Api(api_client), AppsV1Api(api_client), workspace, cluster


def list_nodes_via_k8s(core_api: CoreV1Api) -> list[dict[str, Any]]:
    """List nodes from child cluster via K8s API.

    Returns:
        List of node info dicts.
    """
    try:
        node_list = core_api.list_node()
        nodes = []
        for node in node_list.items:
            labels = node.metadata.labels or {}
            conditions = {c.type: c.status for c in (node.status.conditions or [])}
            allocatable = node.status.allocatable or {}

            nodes.append(
                {
                    "name": node.metadata.name,
                    "labels": labels,
                    "ready": conditions.get("Ready", "Unknown"),
                    "cpu_allocatable": allocatable.get("cpu", "0"),
                    "memory_allocatable": allocatable.get("memory", "0"),
                    "gpu_allocatable": allocatable.get("nvidia.com/gpu", "0"),
                    "instance_type": labels.get(
                        "node.kubernetes.io/instance-type", "unknown"
                    ),
                    "zone": labels.get("topology.kubernetes.io/zone", "unknown"),
                    "created_at": (
                        node.metadata.creation_timestamp.isoformat()
                        if node.metadata.creation_timestamp
                        else None
                    ),
                }
            )
        return nodes
    except ApiException as exc:
        logger.error("K8s list_node failed: %s", exc)
        raise ProxyError(
            f"Failed to list nodes from child cluster: {exc.reason}"
        ) from exc


def create_deployment_manifest(
    name: str,
    model_name: str,
    precision: str = "fp16",
    serving_framework: str = "vllm",
    replicas: int = 1,
    gpu_per_replica: int = 1,
    tensor_parallel_size: int = 1,
    max_model_len: int | None = None,
    *,
    namespace: str,
    workspace_id: uuid.UUID | str,
) -> dict[str, Any]:
    """Generate a vLLM/SGLang K8s Deployment manifest.

    Args:
        name: Deployment name.
        model_name: HuggingFace model name (e.g., meta-llama/Llama-3.1-8B-Instruct).
        precision: Model precision (fp16, bf16, fp8, awq).
        serving_framework: vllm or sglang.
        replicas: Number of replicas.
        gpu_per_replica: GPUs per replica.
        tensor_parallel_size: Tensor parallel degree.
        max_model_len: Maximum model context length.
        namespace: K8s namespace.

    Returns:
        K8s Deployment manifest as dict.
    """
    # Container image selection
    images = {
        "vllm": "vllm/vllm-openai:latest",
        "sglang": "lmsysorg/sglang:latest",
    }
    image = images.get(serving_framework, images["vllm"])

    # Build container args
    if serving_framework == "vllm":
        args = [
            "--model",
            model_name,
            "--dtype",
            precision,
            "--tensor-parallel-size",
            str(tensor_parallel_size),
            "--port",
            "8000",
        ]
        if max_model_len:
            args.extend(["--max-model-len", str(max_model_len)])
    else:  # sglang
        args = [
            "--model-path",
            model_name,
            "--dtype",
            precision,
            "--tp",
            str(tensor_parallel_size),
            "--port",
            "8000",
        ]
        if max_model_len:
            args.extend(["--context-length", str(max_model_len)])

    manifest = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app": name,
                "superplane.io/component": "model-serving",
                WORKSPACE_OWNER_LABEL: str(workspace_id),
                "superplane.io/framework": serving_framework,
                "superplane.io/model": model_name.replace("/", "--"),
            },
        },
        "spec": {
            "replicas": replicas,
            "selector": {
                "matchLabels": {"app": name},
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app": name,
                        "superplane.io/component": "model-serving",
                    },
                },
                "spec": {
                    "containers": [
                        {
                            "name": serving_framework,
                            "image": image,
                            "args": args,
                            "ports": [{"containerPort": 8000, "name": "http"}],
                            "resources": {
                                "limits": {
                                    "nvidia.com/gpu": str(gpu_per_replica),
                                },
                                "requests": {
                                    "nvidia.com/gpu": str(gpu_per_replica),
                                },
                            },
                            "env": [
                                {
                                    "name": "HUGGING_FACE_HUB_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": "hf-token",
                                            "key": "token",
                                            "optional": True,
                                        },
                                    },
                                },
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": 8000},
                                "initialDelaySeconds": 60,
                                "periodSeconds": 10,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/health", "port": 8000},
                                "initialDelaySeconds": 120,
                                "periodSeconds": 30,
                            },
                        }
                    ],
                    "tolerations": [
                        {
                            "key": "nvidia.com/gpu",
                            "operator": "Exists",
                            "effect": "NoSchedule",
                        }
                    ],
                },
            },
        },
    }

    return manifest


def apply_deployment_via_k8s(
    apps_api: AppsV1Api,
    manifest: dict[str, Any],
    *,
    expected_uid: str | None = None,
) -> dict[str, Any]:
    """Create once or observe this operation's unchanged result; never replace."""
    if not has_create_binding(manifest):
        raise ProxyError(
            "Deployment create requires a durable operation binding", status_code=409
        )
    namespace = manifest["metadata"].get("namespace", "default")
    name = manifest["metadata"]["name"]

    def observed_result(result):
        with ApiClient() as serializer:
            observed = serializer.sanitize_for_serialization(result)
        if not matches_create(observed, manifest):
            raise ProxyError(
                "Deployment name is occupied by a different or changed operation",
                status_code=409,
            )
        if expected_uid is not None and observed["metadata"]["uid"] != expected_uid:
            raise ProxyError("Deployment immutable UID changed", status_code=409)
        return {
            "name": observed["metadata"]["name"],
            "namespace": observed["metadata"]["namespace"],
            "replicas": observed["spec"]["replicas"],
            "status": "Created",
            "provider_uid": observed["metadata"]["uid"],
        }

    try:
        try:
            existing = apps_api.read_namespaced_deployment(
                name=name, namespace=namespace
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
            if expected_uid is not None:
                raise ProxyError(
                    "Previously observed deployment is absent; refusing recreation",
                    status_code=409,
                ) from exc
        else:
            return observed_result(existing)
        try:
            result = apps_api.create_namespaced_deployment(
                namespace=namespace, body=manifest
            )
        except ApiException as exc:
            if exc.status != 409:
                raise
            # Another request may have created the name after the absence read.
            # Ownership and generation are checked exactly as on a restart.
            result = apps_api.read_namespaced_deployment(name=name, namespace=namespace)
        return observed_result(result)
    except ApiException as exc:
        raise ProxyError(
            "Workspace deployment could not be observed or created",
            status_code=403 if exc.status in (401, 403) else 502,
        ) from exc


def list_deployments_via_k8s(
    apps_api: AppsV1Api,
    *,
    namespace: str,
    workspace_id: uuid.UUID | str,
) -> list[dict[str, Any]]:
    """List only this workspace's model-serving objects in its recorded namespace."""
    label_selector = (
        f"superplane.io/component=model-serving,{WORKSPACE_OWNER_LABEL}={workspace_id}"
    )
    try:
        dep_list = apps_api.list_namespaced_deployment(
            namespace=namespace,
            label_selector=label_selector,
        )
        deployments = []
        for dep in dep_list.items:
            deployments.append(
                {
                    "name": dep.metadata.name,
                    "provider_uid": dep.metadata.uid,
                    "namespace": dep.metadata.namespace,
                    "replicas": dep.spec.replicas,
                    "ready_replicas": dep.status.ready_replicas or 0,
                    "available_replicas": dep.status.available_replicas or 0,
                    "labels": dep.metadata.labels or {},
                    "created_at": (
                        dep.metadata.creation_timestamp.isoformat()
                        if dep.metadata.creation_timestamp
                        else None
                    ),
                }
            )
        return deployments
    except ApiException as exc:
        logger.error("K8s list_deployments failed: %s", exc)
        raise ProxyError(
            f"Failed to list deployments from child cluster: {exc.reason}"
        ) from exc


def get_deployment_uid_via_k8s(
    apps_api: AppsV1Api, manifest: dict[str, Any]
) -> str | None:
    """Observe the owned create result so callers can persist its UID before delete."""
    try:
        observed = apps_api.read_namespaced_deployment(
            name=manifest["metadata"]["name"],
            namespace=manifest["metadata"].get("namespace", "default"),
        )
        with ApiClient() as serializer:
            observed = serializer.sanitize_for_serialization(observed)
        if not matches_create(observed, manifest):
            raise ProxyError(
                "Deployment ownership is unavailable; refusing deletion by name",
                status_code=409,
            )
        return observed["metadata"]["uid"]
    except ApiException as exc:
        if exc.status == 404:
            return None
        raise ProxyError("Workspace deployment identity could not be observed") from exc


def delete_deployment_via_k8s(
    apps_api: AppsV1Api,
    name: str,
    namespace: str = "default",
    *,
    expected_uid: str | None = None,
    expected_manifest: dict | None = None,
    absent_ok: bool = False,
) -> dict[str, str]:
    """Delete a deployment from child cluster via K8s API.

    Returns:
        Deletion status dict.
    """
    try:
        if expected_uid is None and expected_manifest is not None:
            observed = apps_api.read_namespaced_deployment(
                name=name, namespace=namespace
            )
            with ApiClient() as serializer:
                observed = serializer.sanitize_for_serialization(observed)
            if not matches_create(observed, expected_manifest):
                raise ProxyError(
                    "Deployment ownership is unavailable; refusing deletion by name",
                    status_code=409,
                )
            expected_uid = observed["metadata"]["uid"]
        arguments = {"name": name, "namespace": namespace}
        if expected_uid is not None:
            arguments["body"] = {"preconditions": {"uid": expected_uid}}
        apps_api.delete_namespaced_deployment(**arguments)
        # Kubernetes acknowledges asynchronous deletion before finalizers finish.
        # Hold the allocation until a read proves the object is absent.
        apps_api.read_namespaced_deployment(name=name, namespace=namespace)
        return {"name": name, "namespace": namespace, "status": "Deleting"}
    except ApiException as exc:
        if exc.status == 404:
            if absent_ok:
                return {"name": name, "namespace": namespace, "status": "Deleted"}
            raise ProxyError(f"Deployment '{name}' not found", status_code=404) from exc
        logger.error("K8s delete_deployment failed: %s", exc)
        raise ProxyError(
            f"Failed to delete deployment from child cluster: {exc.reason}"
        ) from exc
