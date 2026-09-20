"""Read actual revisions through the already-selected registered AWS role."""

import json
import re
import subprocess

from .http import Unsupported


def read_runtime(client, target):
    import boto3
    from botocore.config import Config

    bounded = Config(
        connect_timeout=3, read_timeout=10, retries={"total_max_attempts": 1}
    )
    session = boto3.Session()
    identity = session.client("sts", config=bounded).get_caller_identity()
    rows = client.get("/auth/credentials")
    row = next((r for r in rows if r["id"] == client.config.connection_ref), None)
    scopes = (row or {}).get("scopes") or {}
    role = scopes.get("role_arn", "")
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/(.+)", role)
    if (
        not match
        or scopes.get("status") != "verified"
        or identity["Account"] != client.config.expected_account_id
        or match[1] != identity["Account"]
        or not identity["Arn"].startswith(
            f"arn:aws:sts::{match[1]}:assumed-role/{match[2].split('/')[-1]}/"
        )
    ):
        raise Unsupported(
            "runtime reads require the registered role; ambient platform credentials are not a fallback"
        )
    cluster = session.client("eks", config=bounded).describe_cluster(
        name=target.cluster
    )["cluster"]
    context = cluster["arn"]
    if context.split(":")[4] != identity["Account"]:
        raise Unsupported("cluster account mismatch")

    def read(*args):
        try:
            raw = subprocess.check_output(
                [
                    "kubectl",
                    "--context",
                    context,
                    "--request-timeout=15s",
                    "-n",
                    target.namespace,
                    "get",
                    *args,
                    "-o",
                    "json",
                ],
                timeout=20,
                stderr=subprocess.DEVNULL,
            )
            return json.loads(raw)
        except (OSError, subprocess.SubprocessError):
            raise Unsupported("scoped Kubernetes read unavailable") from None

    deployment = read("deployment", target.deployment)
    selector = deployment["spec"]["selector"].get("matchLabels")
    if not selector:
        raise Unsupported("runtime selector is unavailable")
    pods = read(
        "pods", "-l", ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    )["items"]
    active = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
    desired = deployment["spec"]["replicas"]
    status = deployment["status"]
    assert (
        desired > 0
        and status["observedGeneration"] == deployment["metadata"]["generation"]
    )
    assert status.get("updatedReplicas") == status.get("availableReplicas") == desired
    assert len(active) >= desired
    digests = set()
    for pod in active:
        assert any(
            c["type"] == "Ready" and c["status"] == "True"
            for c in pod["status"]["conditions"]
        )
        container = next(
            c
            for c in pod["status"]["containerStatuses"]
            if c["name"] == target.container
        )
        assert container["ready"]
        image = container["imageID"]
        assert "@sha256:" in image
        digests.add(image.split("@", 1)[1])
    assert len(digests) == 1
    image_digest = next(iter(digests))
    detail = session.client("ecr", config=bounded).describe_images(
        repositoryName=target.ecr_repository, imageIds=[{"imageDigest": image_digest}]
    )["imageDetails"]
    revisions = {
        tag
        for row in detail
        for tag in row.get("imageTags", [])
        if re.fullmatch(r"[0-9a-f]{40}", tag)
    }
    if len(revisions) != 1:
        raise Unsupported(
            "runtime digest does not resolve to one immutable source revision"
        )
    return {
        "actual_revision": next(iter(revisions)),
        "digest": image_digest,
        "account_id": identity["Account"],
        "role_arn": role,
        "cluster": context,
        "namespace": target.namespace,
        "deployment": target.deployment,
        "generation": deployment["metadata"]["generation"],
        "ready_replicas": desired,
        "pods": [p["metadata"]["uid"] for p in active],
    }
