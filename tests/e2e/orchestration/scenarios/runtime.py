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


def read_runtime(
    client, target, *, repository=None, source_revision=None, workflow_path=None
):
    if target.kind == "job":
        return read_completed_job(
            client, target, repository, source_revision, workflow_path
        )
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


def read_completed_job(client, target, repository, source_revision, workflow_path):
    if (
        not isinstance(repository, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
        or not isinstance(source_revision, str)
        or not re.fullmatch(r"[0-9a-f]{40}", source_revision)
        or not isinstance(workflow_path, str)
        or not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.ya?ml", workflow_path)
    ):
        raise Unsupported(
            "source client Job requires a reviewed repository, revision and workflow"
        )
    runs = client.pages(
        f"/repos/{repository}/actions/runs?head_sha={source_revision}",
        key="workflow_runs",
    )
    matches = [run for run in runs if run.get("path") == workflow_path]
    if len(matches) != 1:
        raise Unsupported("source client workflow identity is missing or ambiguous")
    workflow = matches[0]
    if (
        workflow.get("head_sha") != source_revision
        or workflow.get("repository", {}).get("full_name") != repository
        or workflow.get("status") != "completed"
        or workflow.get("conclusion") != "success"
        or type(workflow.get("id")) is not int
        or type(workflow.get("run_attempt")) is not int
        or workflow["id"] < 1
        or workflow["run_attempt"] < 1
    ):
        raise Unsupported("source client workflow completion or attribution unverified")
    job_name = f"client-{workflow['id']}-{workflow['run_attempt']}"
    kube = ScopedKubernetes(client, target.cluster)
    path = f"/apis/batch/v1/namespaces/{target.namespace}/jobs/{job_name}"
    job = kube.get(path)
    metadata = job["metadata"]
    status = job["status"]
    spec = job["spec"]
    annotations = metadata.get("annotations") or {}
    run_id = annotations.get("adp/workflow-run-id")
    run_attempt = annotations.get("adp/workflow-run-attempt")
    if (
        metadata.get("name") != job_name
        or metadata.get("namespace") != target.namespace
        or not metadata.get("uid")
        or metadata.get("deletionTimestamp")
        or status.get("succeeded") != 1
        or status.get("failed", 0) != 0
        or status.get("active", 0) != 0
        or not any(
            condition.get("type") == "Complete" and condition.get("status") == "True"
            for condition in status.get("conditions", [])
        )
        or spec.get("completions", 1) != 1
        or spec.get("parallelism", 1) != 1
        or spec["template"]["spec"].get("restartPolicy") != "Never"
        or run_id != str(workflow["id"])
        or run_attempt != str(workflow["run_attempt"])
        or annotations.get("adp/source-revision") != source_revision
    ):
        raise Unsupported(
            "source client Job completion or workflow attribution unverified"
        )
    container = next(
        item
        for item in spec["template"]["spec"]["containers"]
        if item["name"] == target.container
    )
    digest = image_reference(
        container["image"],
        kube.identity["Account"],
        kube.cluster["arn"].split(":")[3],
        target.ecr_repository,
    ).get("imageDigest")
    if digest is None:
        raise Unsupported("source client Job requires an immutable ECR image digest")
    pods = kube.get(
        f"/api/v1/namespaces/{target.namespace}/pods?labelSelector="
        f"{quote('job-name=' + job_name, safe='')}&limit=2"
    )
    if pods.get("metadata", {}).get("continue") or len(pods.get("items", [])) != 1:
        raise Unsupported("source client Job Pod identity is ambiguous")
    pod = pods["items"][0]
    pod_metadata = pod["metadata"]
    pod_status = pod["status"]
    if (
        pod_metadata.get("namespace") != target.namespace
        or not pod_metadata.get("uid")
        or pod_metadata.get("deletionTimestamp")
        or not any(
            ref.get("uid") == metadata["uid"]
            and ref.get("controller") is True
            and ref.get("kind") == "Job"
            and ref.get("name") == job_name
            for ref in pod_metadata.get("ownerReferences", [])
        )
        or pod_status.get("phase") != "Succeeded"
        or len(pod.get("spec", {}).get("containers", [])) != 1
        or pod["spec"]["containers"][0].get("image") != container["image"]
        or pod["spec"]["containers"][0].get("name") != target.container
        or len(pod_status.get("containerStatuses", [])) != 1
        or pod_status["containerStatuses"][0].get("name") != target.container
        or pod_status["containerStatuses"][0]
        .get("state", {})
        .get("terminated", {})
        .get("exitCode")
        != 0
        or not pod_status["containerStatuses"][0]
        .get("imageID", "")
        .endswith("@" + digest)
    ):
        raise Unsupported("source client Job Pod or image is unverified")
    image = kube.session.client("ecr", config=kube.bounded).describe_images(
        repositoryName=target.ecr_repository,
        imageIds=[{"imageDigest": digest}],
    )["imageDetails"]
    if len(image) != 1 or image[0].get("imageDigest") != digest:
        raise Unsupported("source client ECR digest not verified")
    revisions = {
        tag
        for tag in image[0].get("imageTags", [])
        if re.fullmatch(r"[0-9a-f]{40}", tag)
    }
    if revisions != {annotations["adp/source-revision"]}:
        raise Unsupported("source client ECR revision differs from Job attribution")
    return {
        "actual_revision": annotations["adp/source-revision"],
        "digest": digest,
        "account_id": kube.identity["Account"],
        "role_arn": kube.role,
        "cluster": kube.cluster["arn"],
        "namespace": target.namespace,
        "deployment": job_name,
        "job_uid": metadata["uid"],
        "kind": "job",
        "pods": [pod_metadata["uid"]],
        "workflow_run_id": int(run_id),
        "workflow_run_attempt": int(run_attempt),
        "scope": "verified completed source client Job",
    }
