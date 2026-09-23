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
from kubernetes.client import Configuration, ApiClient, CoreV1Api, AppsV1Api
from kubernetes.client.rest import ApiException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.cloud_account import CloudAccount
from app.models.cluster import Cluster
from app.models.workspace import Workspace
from app.services.eks_auth import (
    EksAuthError,
    build_cluster_token,
    describe_cluster_ca,
    write_ca_bundle,
)

logger = logging.getLogger(__name__)

# STS token duration (15 minutes — minimum)
TOKEN_DURATION_SECONDS = 900

# The label carrying the workspace a model-serving object belongs to (issue #5671, A15).
#
# Ownership has to be recorded ON the object because the mutating calls are made with
# workspace-brokered credentials against a cluster that may host several workspaces:
# reaching the right namespace is necessary but not sufficient, since a namespace can
# contain an object the platform never created for that workspace. Replace and delete
# therefore read this label back and compare it before acting, which is the same
# predicate the listing path already applied via its label selector.
WORKSPACE_OWNER_LABEL = "superplane.io/workspace"

# The component label every model-serving object created here carries. Used by the
# listing path's selector and by the ownership checks below.
MODEL_SERVING_SELECTOR = "superplane.io/component=model-serving"


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

    ``namespace`` and ``workspace_id`` are keyword-only and have NO defaults (issue
    #5671, A15). They used to default to ``"default"``, which meant a caller-supplied
    namespace — or an omitted one — placed tenant workloads in the cluster's shared
    namespace. Requiring both at the call site means a new entry point cannot reach
    this function without stating, explicitly, which workspace it is acting for.

    Args:
        name: Deployment name.
        model_name: HuggingFace model name (e.g., meta-llama/Llama-3.1-8B-Instruct).
        precision: Model precision (fp16, bf16, fp8, awq).
        serving_framework: vllm or sglang.
        replicas: Number of replicas.
        gpu_per_replica: GPUs per replica.
        tensor_parallel_size: Tensor parallel degree.
        max_model_len: Maximum model context length.
        namespace: K8s namespace, resolved server-side from the owning workspace.
        workspace_id: Owning workspace; stamped as the ownership label that
            replace/delete later verify before touching the object.

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
                # The ownership marker replace/delete verify before mutating an
                # existing object (issue #5671, A15).
                WORKSPACE_OWNER_LABEL: str(workspace_id),
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


def _assert_owned_by_workspace(
    apps_api: AppsV1Api,
    name: str,
    namespace: str,
    workspace_id: uuid.UUID | str,
) -> None:
    """Refuse unless the existing deployment carries this workspace's ownership label.

    Applied before replace and before delete (issue #5671, A15). Being in the right
    namespace is not proof of ownership: a shared cluster's namespace can hold an
    object the platform did not create for this workspace, and overwriting or removing
    it takes down a workload whose owner gets no explanation.

    A missing label is treated as NOT owned. The alternative — adopting unlabelled
    objects — would mean any object the platform did not create becomes mutable by
    whichever workspace names it, which is the defect rather than a lenient reading
    of it.

    Raises:
        ProxyError: 404 if there is nothing there, 409 if it belongs to someone else.
            Deliberately not 403: the requester is authorized for their workspace, and
            the object's existence is not theirs to learn about.
    """
    try:
        existing = apps_api.read_namespaced_deployment(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            raise ProxyError(
                f"Deployment '{name}' not found", status_code=404
            ) from exc
        logger.error("K8s read_deployment failed during ownership check: %s", exc)
        raise ProxyError(
            f"Failed to verify deployment ownership on child cluster: {exc.reason}"
        ) from exc

    labels = (getattr(existing, "metadata", None) and existing.metadata.labels) or {}
    owner = labels.get(WORKSPACE_OWNER_LABEL)
    if owner != str(workspace_id):
        logger.warning(
            "Refusing mutation of deployment %s/%s: owner label %r != workspace %s",
            namespace,
            name,
            owner,
            workspace_id,
        )
        raise ProxyError(
            f"Deployment '{name}' is not owned by this workspace",
            status_code=409,
        )


def apply_deployment_via_k8s(
    apps_api: AppsV1Api,
    manifest: dict[str, Any],
    *,
    workspace_id: uuid.UUID | str,
) -> dict[str, Any]:
    """Apply a K8s Deployment manifest to the child cluster.

    ``workspace_id`` is required (issue #5671, A15): on the conflict-then-replace
    path an object already exists under that name, and replacing it without checking
    ownership is how one workspace overwrites another's workload on a shared cluster.

    Returns:
        Deployment status dict.
    """
    # Read the namespace from the manifest rather than accepting a parameter: the
    # manifest was built by `create_deployment_manifest`, whose namespace is already
    # server-resolved, so there is no second place for a caller-derived value to enter.
    namespace = manifest["metadata"]["namespace"]
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
            # Already exists. Replace it only if this workspace owns it — otherwise
            # the conflict is reported to the caller, not resolved by overwriting.
            _assert_owned_by_workspace(apps_api, name, namespace, workspace_id)
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
    *,
    namespace: str,
    workspace_id: uuid.UUID | str,
) -> list[dict[str, Any]]:
    """List deployments from child cluster via K8s API.

    Scoped to the workspace's own namespace AND to objects carrying its ownership
    label (issue #5671, A15). The selector previously matched every model-serving
    object in whatever namespace was asked for, so on a shared cluster a caller could
    enumerate a neighbour's workloads. Both arguments are server-resolved.

    Returns:
        List of deployment info dicts.
    """
    label_selector = f"{MODEL_SERVING_SELECTOR},{WORKSPACE_OWNER_LABEL}={workspace_id}"
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
    *,
    namespace: str,
    workspace_id: uuid.UUID | str,
) -> dict[str, str]:
    """Delete a deployment from child cluster via K8s API.

    ``namespace`` and ``workspace_id`` are keyword-only and required (issue #5671,
    A15). ``namespace`` used to default to ``"default"``, so an omitted value deleted
    out of the cluster's shared namespace; the ownership label is now verified first,
    so a delete cannot remove an object this workspace does not own.

    Returns:
        Deletion status dict.
    """
    _assert_owned_by_workspace(apps_api, name, namespace, workspace_id)

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
