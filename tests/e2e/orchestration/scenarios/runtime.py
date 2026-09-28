"""Read actual revisions through the registered user's exact AWS session."""

import re
from urllib.parse import quote

from .http import Unsupported
from .kubernetes import ScopedKubernetes


def image_reference(image, account, region, repository):
    prefix = f"{account}.dkr.ecr.{region}.amazonaws.com/{repository}"
    if image.startswith(prefix + "@sha256:") and re.fullmatch(
        r"[0-9a-f]{64}", image[len(prefix) + 8 :]
    ):
        return {"imageDigest": image[len(prefix) + 1 :]}
    if image.startswith(prefix + ":") and re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", image[len(prefix) + 1 :]
    ):
        return {"imageTag": image[len(prefix) + 1 :]}
    raise Unsupported(
        "runtime image must name the exact registered ECR account, region and repository"
    )


def read_runtime(client, target):
    kube = ScopedKubernetes(client, target.cluster)
    ecr = kube.session.client("ecr", config=kube.bounded)
    active, desired = [], 0
    if target.kind == "scaledjob":
        workload = kube.get(
            f"/apis/keda.sh/v1alpha1/namespaces/{target.namespace}/scaledjobs/{target.deployment}"
        )
        containers = workload["spec"]["jobTargetRef"]["template"]["spec"]["containers"]
        image = next(c["image"] for c in containers if c["name"] == target.container)
        image_id = image_reference(
            image,
            kube.identity["Account"],
            kube.cluster["arn"].split(":")[3],
            target.ecr_repository,
        )
        detail = ecr.describe_images(
            repositoryName=target.ecr_repository, imageIds=[image_id]
        )["imageDetails"]
    else:
        workload = kube.get(
            f"/apis/apps/v1/namespaces/{target.namespace}/deployments/{target.deployment}"
        )
        selector = workload["spec"]["selector"].get("matchLabels")
        if not selector:
            raise Unsupported("runtime selector is unavailable")
        label_selector = quote(
            ",".join(f"{k}={v}" for k, v in sorted(selector.items())), safe=""
        )
        pods = kube.get(
            f"/api/v1/namespaces/{target.namespace}/pods?labelSelector={label_selector}"
        )["items"]
        active = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        if len(active) > 100:
            raise Unsupported("runtime observation resource bound exceeded")
        desired = workload["spec"]["replicas"]
        status = workload["status"]
        assert (
            desired > 0
            and status["observedGeneration"] == workload["metadata"]["generation"]
        )
        assert (
            status.get("updatedReplicas") == status.get("availableReplicas") == desired
        )
        assert len(active) >= desired
        digests, replicasets = set(), {}
        for pod in active:
            assert any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in pod["status"]["conditions"]
            )
            owner = next(
                r
                for r in pod["metadata"]["ownerReferences"]
                if r.get("controller") and r["kind"] == "ReplicaSet"
            )
            if owner["name"] not in replicasets:
                replicasets[owner["name"]] = kube.get(
                    f"/apis/apps/v1/namespaces/{target.namespace}/replicasets/{owner['name']}"
                )
            rs = replicasets[owner["name"]]["metadata"]
            assert rs["uid"] == owner["uid"]
            assert any(
                r["kind"] == "Deployment" and r["uid"] == workload["metadata"]["uid"]
                for r in rs["ownerReferences"]
            )
            container = next(
                c
                for c in pod["status"]["containerStatuses"]
                if c["name"] == target.container
            )
            assert container["ready"] and "@sha256:" in container["imageID"]
            image_reference(
                container["imageID"].removeprefix("docker-pullable://"),
                kube.identity["Account"],
                kube.cluster["arn"].split(":")[3],
                target.ecr_repository,
            )
            digests.add(container["imageID"].split("@", 1)[1])
        assert len(digests) == 1
        detail = ecr.describe_images(
            repositoryName=target.ecr_repository,
            imageIds=[{"imageDigest": next(iter(digests))}],
        )["imageDetails"]
    assert len(detail) == 1
    revisions = {
        tag
        for tag in detail[0].get("imageTags", [])
        if re.fullmatch(r"[0-9a-f]{40}", tag)
    }
    if len(revisions) != 1:
        raise Unsupported(
            "runtime digest does not resolve to one immutable source revision"
        )
    return {
        "actual_revision": next(iter(revisions)),
        "digest": detail[0]["imageDigest"],
        "account_id": kube.identity["Account"],
        "role_arn": kube.role,
        "cluster": kube.cluster["arn"],
        "namespace": target.namespace,
        "deployment": target.deployment,
        "kind": target.kind,
        "generation": workload["metadata"]["generation"],
        "ready_replicas": desired,
        "pods": [p["metadata"]["uid"] for p in active],
        "pod_names": {p["metadata"]["name"]: p["metadata"]["uid"] for p in active},
        "scope": "worker template revision"
        if target.kind == "scaledjob"
        else "ready deployed replicas",
    }
