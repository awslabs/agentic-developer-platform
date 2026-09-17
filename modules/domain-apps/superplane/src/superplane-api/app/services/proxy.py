"""Kubernetes proxy service — assumes cross-account IAM role and creates K8s client for child clusters.

Pattern: look up cluster in DB -> assume cross-account IAM role (boto3 STS)
-> create K8s client for child cluster -> forward request -> return response.
"""

import base64
import logging
import tempfile
import uuid
from typing import Any

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
from kubernetes.client import Configuration, ApiClient, CoreV1Api, AppsV1Api
from kubernetes.client.rest import ApiException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.cluster import Cluster
from app.models.workspace import Workspace

logger = logging.getLogger(__name__)

# STS token duration (15 minutes — minimum)
TOKEN_DURATION_SECONDS = 900


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
) -> tuple[Workspace, Cluster]:
    """Fetch workspace and its associated cluster, validating ownership.

    Raises:
        ProxyError: If workspace not found, not active, or no cluster attached.
    """
    result = await db.execute(
        select(Workspace).where(
            Workspace.id == workspace_id,
            Workspace.org_id == org_id,
        )
    )
    workspace = result.scalar_one_or_none()

    if workspace is None:
        raise ProxyError("Workspace not found", status_code=404)

    if workspace.status != "Active":
        raise ProxyError(
            f"Workspace is not active (status: {workspace.status}). Proxy requires an active workspace.",
            status_code=400,
        )

    if not workspace.cluster_id:
        raise ProxyError("No cluster associated with this workspace", status_code=400)

    cluster_result = await db.execute(
        select(Cluster).where(Cluster.id == workspace.cluster_id)
    )
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


def assume_role_for_cluster(
    aws_account_id: str,
    workspace_name: str,
    session_suffix: str = "proxy",
) -> dict:
    """Assume the cross-account IAM role in the workspace's AWS account.

    Role ARN pattern: arn:aws:iam::{account_id}:role/superplane-workspace-{name}

    Returns:
        dict with AccessKeyId, SecretAccessKey, SessionToken.
    """
    sts_client = boto3.client("sts", region_name=settings.aws_region)
    role_arn = (
        f"arn:aws:iam::{aws_account_id}:role/superplane-workspace-{workspace_name}"
    )

    try:
        response = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName=f"superplane-{session_suffix}-{workspace_name}",
            DurationSeconds=TOKEN_DURATION_SECONDS,
        )
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
) -> ApiClient:
    """Create a Kubernetes API client using assumed-role credentials.

    Args:
        cluster_endpoint: EKS API server endpoint URL.
        cluster_ca_data: Base64-encoded CA certificate data.
        credentials: STS assumed-role credentials dict.

    Returns:
        Configured kubernetes ApiClient.
    """
    configuration = Configuration()
    configuration.host = cluster_endpoint
    configuration.api_key = {"BearerToken": credentials["SessionToken"]}
    configuration.api_key_prefix = {"BearerToken": "Bearer"}

    # Write CA cert to a temp file for TLS verification
    if cluster_ca_data:
        ca_bytes = base64.b64decode(cluster_ca_data)
        ca_file = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
        ca_file.write(ca_bytes)
        ca_file.flush()
        configuration.ssl_ca_cert = ca_file.name
    else:
        # In dev/test, allow insecure connections
        configuration.verify_ssl = False

    return ApiClient(configuration)


async def get_k8s_clients(
    workspace_id: uuid.UUID,
    org_id: uuid.UUID,
    db: AsyncSession,
) -> tuple[CoreV1Api, AppsV1Api, Workspace, Cluster]:
    """Full proxy setup: look up cluster, assume role, create K8s clients.

    Returns:
        Tuple of (CoreV1Api, AppsV1Api, workspace, cluster).
    """
    workspace, cluster = await get_workspace_cluster(workspace_id, org_id, db)

    aws_account_id = _get_aws_account_from_cluster(cluster)
    if not aws_account_id:
        raise ProxyError("Cannot determine AWS account for cluster proxy")

    credentials = assume_role_for_cluster(aws_account_id, workspace.name)

    # Cluster CA data would come from cluster metadata; use empty string as fallback
    ca_data = ""  # In production, stored in cluster metadata or Secrets Manager

    api_client = create_k8s_client(
        cluster_endpoint=cluster.endpoint or "",
        cluster_ca_data=ca_data,
        credentials=credentials,
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
    namespace: str = "default",
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
) -> dict[str, Any]:
    """Apply a K8s Deployment manifest to the child cluster.

    Returns:
        Deployment status dict.
    """
    namespace = manifest["metadata"].get("namespace", "default")
    name = manifest["metadata"]["name"]

    try:
        # Try to create first
        result = apps_api.create_namespaced_deployment(
            namespace=namespace,
            body=manifest,
        )
        return {
            "name": result.metadata.name,
            "namespace": result.metadata.namespace,
            "replicas": result.spec.replicas,
            "status": "Created",
        }
    except ApiException as exc:
        if exc.status == 409:
            # Already exists — update via replace
            try:
                result = apps_api.replace_namespaced_deployment(
                    name=name,
                    namespace=namespace,
                    body=manifest,
                )
                return {
                    "name": result.metadata.name,
                    "namespace": result.metadata.namespace,
                    "replicas": result.spec.replicas,
                    "status": "Updated",
                }
            except ApiException as update_exc:
                logger.error("K8s replace_deployment failed: %s", update_exc)
                raise ProxyError(
                    f"Failed to update deployment on child cluster: {update_exc.reason}"
                ) from update_exc
        logger.error("K8s create_deployment failed: %s", exc)
        raise ProxyError(
            f"Failed to create deployment on child cluster: {exc.reason}"
        ) from exc


def list_deployments_via_k8s(
    apps_api: AppsV1Api,
    namespace: str = "default",
    label_selector: str = "superplane.io/component=model-serving",
) -> list[dict[str, Any]]:
    """List deployments from child cluster via K8s API.

    Returns:
        List of deployment info dicts.
    """
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


def delete_deployment_via_k8s(
    apps_api: AppsV1Api,
    name: str,
    namespace: str = "default",
) -> dict[str, str]:
    """Delete a deployment from child cluster via K8s API.

    Returns:
        Deletion status dict.
    """
    try:
        apps_api.delete_namespaced_deployment(
            name=name,
            namespace=namespace,
        )
        return {"name": name, "namespace": namespace, "status": "Deleted"}
    except ApiException as exc:
        if exc.status == 404:
            raise ProxyError(f"Deployment '{name}' not found", status_code=404) from exc
        logger.error("K8s delete_deployment failed: %s", exc)
        raise ProxyError(
            f"Failed to delete deployment from child cluster: {exc.reason}"
        ) from exc
