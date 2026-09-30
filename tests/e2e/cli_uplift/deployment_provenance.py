"""Bind the live gateway Deployment to reviewed immutable build evidence.

The orchestration Lambda can deploy independently; it is not the gateway oracle.
Only two named Kubernetes objects are read. No pod listing, exec, or secrets.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import ssl
import urllib.request

from .ports import PortError

CATALOG = (
    Path(__file__).resolve().parents[3]
    / "docs/regression-testing/cli-uplift/gateway-deployment-receipts.json"
)


def require(value, message):
    if not value:
        raise PortError(message)


def binding(cfg):
    name = cfg.get("gateway_deployment")
    # Public receipts are sanitized examples. Live jobs receive the reviewed
    # catalog through their protected GitHub environment, never through docs.
    raw = os.environ.get("CLI_UPLIFT_EVAL_GATEWAY_CATALOG", "").strip()
    try:
        catalog = json.loads(raw if raw else CATALOG.read_text())
    except (ValueError, OSError):
        raise PortError(
            "Gateway deployment catalog is unavailable or invalid"
        ) from None
    require(isinstance(catalog, dict), "Gateway deployment catalog must be an object")
    require(
        isinstance(name, str) and name in catalog, "Unknown gateway deployment binding"
    )
    record = catalog[name]
    require(
        cfg["gateway_url"].rstrip("/") == record["gateway_url"]
        and cfg["platform_account"] == record["account"]
        and cfg["region"] == record["region"],
        "Gateway deployment binding does not match evaluation target",
    )
    return record


def eks_bearer(aws, cluster_name, region):
    """Generate the EKS IAM token locally with the current role's credentials."""
    from botocore.auth import SigV4QueryAuth
    from botocore.awsrequest import AWSRequest

    request = AWSRequest(
        method="GET",
        url=f"https://sts.{region}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15",
        headers={"x-k8s-aws-id": cluster_name},
    )
    credentials = aws.session().get_credentials().get_frozen_credentials()
    SigV4QueryAuth(credentials, "sts", region, expires=60).add_auth(request)
    return "k8s-aws-v1." + base64.urlsafe_b64encode(
        request.url.encode()
    ).decode().rstrip("=")


def get_json(url, bearer, ca_data):
    """TLS-verifying bounded GET; never expose token, provider body, or environment."""
    try:
        ca = base64.b64decode(ca_data, validate=True).decode("ascii")
        context = ssl.create_default_context(cadata=ca)
        request = urllib.request.Request(
            url, headers={"Authorization": "Bearer " + bearer}
        )

        # An EKS API should never redirect. Do not forward a bearer to a redirect target.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context), NoRedirect()
        )
        with opener.open(request, timeout=30) as response:
            require(response.status == 200, "Gateway metadata read refused")
            raw = response.read(1024 * 1024 + 1)
            require(len(raw) <= 1024 * 1024, "Gateway metadata exceeds size bound")
            return json.loads(raw)
    except Exception:
        raise PortError("Gateway metadata read failed") from None


def immutable_source(aws, selected, digest):
    """Resolve a modern release through its write-once Git source tag in ECR."""
    repository = selected["image_repository"].split("/", 1)[1]
    repositories = aws.call(
        "ecr", "describe_repositories", repositoryNames=[repository]
    ).get("repositories", [])
    require(len(repositories) == 1, "Gateway ECR repository is ambiguous")
    repo = repositories[0]
    require(
        repo.get("repositoryUri") == selected["image_repository"]
        and repo.get("registryId") == selected["account"]
        and repo.get("repositoryName") == repository
        and repo.get("imageTagMutability") == "IMMUTABLE",
        "Gateway source tags are not immutable in the reviewed repository",
    )
    images = aws.call(
        "ecr",
        "describe_images",
        repositoryName=repository,
        imageIds=[{"imageDigest": digest}],
    ).get("imageDetails", [])
    require(len(images) == 1, "Gateway digest has no unique ECR image")
    image = images[0]
    require(
        image.get("registryId") == selected["account"]
        and image.get("repositoryName") == repository
        and image.get("imageDigest") == digest,
        "Gateway ECR image identity mismatch",
    )
    revisions = [
        tag for tag in image.get("imageTags", []) if re.fullmatch(r"[a-f0-9]{40}", tag)
    ]
    require(
        len(revisions) == 1,
        "Gateway digest has no unique immutable source tag or reviewed build receipt",
    )
    return {
        "source_sha": revisions[0],
        "revision_source": "gateway_eks_immutable_ecr_source",
    }


def resolve(aws, cfg, record):
    selected = binding(cfg)
    cluster = aws.call("eks", "describe_cluster", name=selected["cluster"])["cluster"]
    require(
        cluster.get("arn") == selected["cluster_arn"]
        and cluster.get("name") == selected["cluster"]
        and cluster.get("status") == "ACTIVE",
        "Gateway EKS cluster identity is not the reviewed active cluster",
    )
    endpoint = str(cluster.get("endpoint", ""))
    require(
        re.fullmatch(r"https://[A-Za-z0-9.-]+\.eks\.amazonaws\.com", endpoint),
        "Unexpected EKS API endpoint",
    )
    bearer = eks_bearer(aws, selected["cluster"], selected["region"])
    ca = cluster["certificateAuthority"]["data"]
    namespace, deployment_name, service_name = (
        selected["namespace"],
        selected["deployment"],
        selected["service"],
    )
    deployment = get_json(
        f"{endpoint}/apis/apps/v1/namespaces/{namespace}/deployments/{deployment_name}",
        bearer,
        ca,
    )
    service = get_json(
        f"{endpoint}/api/v1/namespaces/{namespace}/services/{service_name}", bearer, ca
    )
    for obj, kind, name in (
        (deployment, "Deployment", deployment_name),
        (service, "Service", service_name),
    ):
        require(
            obj.get("kind") == kind
            and obj.get("metadata", {}).get("namespace") == namespace
            and obj.get("metadata", {}).get("name") == name
            and not obj["metadata"].get("deletionTimestamp"),
            "Gateway Kubernetes object identity mismatch",
        )
    spec, status = deployment["spec"], deployment.get("status", {})
    desired = spec.get("replicas")
    generation = deployment["metadata"].get("generation")
    require(
        isinstance(desired, int)
        and not isinstance(desired, bool)
        and desired > 0
        and isinstance(generation, int)
        and status.get("observedGeneration") == generation
        and all(
            status.get(k) == desired
            for k in (
                "replicas",
                "updatedReplicas",
                "readyReplicas",
                "availableReplicas",
            )
        )
        and status.get("unavailableReplicas", 0) == 0
        and status.get("terminatingReplicas", 0) == 0
        and not spec.get("paused", False),
        "Gateway deployment is unsettled or degraded",
    )
    conditions = {entry["type"]: entry for entry in status.get("conditions", [])}
    require(
        conditions.get("Available", {}).get("status") == "True"
        and conditions.get("Progressing", {}).get("status") == "True"
        and conditions.get("Progressing", {}).get("reason") == "NewReplicaSetAvailable",
        "Gateway rollout has not completed",
    )
    selector = selected["selector"]
    require(
        spec.get("selector") == {"matchLabels": selector}
        and service.get("spec", {}).get("selector") == selector
        and all(
            spec["template"]["metadata"].get("labels", {}).get(k) == v
            for k, v in selector.items()
        ),
        "Gateway service/deployment selector mismatch",
    )
    containers = spec["template"]["spec"]["containers"]
    candidates = [c for c in containers if c.get("name") == selected["container"]]
    require(len(candidates) == 1, "Gateway container identity is ambiguous")
    image = candidates[0].get("image", "")
    prefix = selected["image_repository"] + "@"
    require(
        image.startswith(prefix),
        "Gateway image is not pinned to the reviewed repository",
    )
    digest = image[len(prefix) :]
    require(
        re.fullmatch(r"sha256:[a-f0-9]{64}", digest),
        "Gateway image is not digest pinned",
    )
    build = selected["images"].get(digest)
    if build is None:
        build = immutable_source(aws, selected, digest)
    revision = build["source_sha"]
    require(
        re.fullmatch(r"[a-f0-9]{40}", revision),
        "Gateway receipt lacks an exact source revision",
    )
    record.update(
        {
            "revision_source": build.get(
                "revision_source", "gateway_eks_build_receipt"
            ),
            "revision_evidence": {
                "cluster_arn": cluster["arn"],
                "namespace": namespace,
                "deployment": deployment_name,
                "deployment_uid": deployment["metadata"].get("uid"),
                "generation": generation,
                "service": service_name,
                "image_digest": digest,
                "build_id": build.get("build_id"),
                "source_archive_sha256": build.get("source_archive_sha256"),
            },
        }
    )
    return revision
