"""Namespace-scoped completion evidence for the original approved batch Pod."""

import ipaddress
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from harness_jobs.identity import OperationRefused


def _resources_equal(actual, expected):
    # Kubernetes canonicalizes quantities (1000m -> 1, 1024Mi -> 1Gi).
    # Compare values, preserving the exact approved resource keys and scopes.
    factors = {
        "": 1,
        "n": Decimal("1e-9"),
        "u": Decimal("1e-6"),
        "m": Decimal("1e-3"),
        "k": 1000,
        "M": 1000**2,
        "G": 1000**3,
        "T": 1000**4,
        "P": 1000**5,
        "E": 1000**6,
        **{suffix + "i": 1024**index for index, suffix in enumerate("KMGTPE", 1)},
    }

    def quantity(raw):
        if type(raw) not in {str, int} or len(str(raw)) > 64:
            raise ValueError("invalid quantity")
        match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]*)?|\.[0-9]+)([numkMGTPE]|[KMGTPE]i)?", str(raw)
        )
        if match is None:
            raise ValueError("unsupported quantity")
        return Decimal(match[1]) * factors[match[2] or ""]

    try:
        return set(actual) == set(expected) and all(
            set(actual[scope]) == set(resources)
            and all(
                quantity(actual[scope][name]) == quantity(value)
                for name, value in resources.items()
            )
            for scope, resources in expected.items()
        )
    except (KeyError, TypeError, ValueError, InvalidOperation):
        return False


async def completed_batch(workspace, operation, target, plan, job, authorize):
    """A Job counter alone is not proof the approved ordinary Pod executed.

    This observes no cluster-wide resources and claims no DNS/Service traffic
    proof. Exact node identity remains the separate allocation readiness gate.
    """
    uid = job["metadata"]["uid"]
    spec = plan.data["workload"]
    await authorize()
    selector = quote("batch.kubernetes.io/controller-uid=" + uid, safe="")
    response = await workspace.request(
        operation,
        target,
        "GET",
        workspace.path(target, "Pod") + "?limit=32&labelSelector=" + selector,
    )
    if response.status_code != 200:
        return False
    listing = response.json()
    items = listing.get("items")
    if (
        not isinstance(items, list)
        or len(items) > 32
        or listing.get("metadata", {}).get("continue")
    ):
        raise OperationRefused("completed Pod inventory is incomplete")
    completed = []
    try:
        for pod in items:
            metadata = pod.get("metadata", {})
            controllers = [
                owner
                for owner in metadata.get("ownerReferences", [])
                if owner.get("controller") is True
            ]
            if not any(owner.get("uid") == uid for owner in controllers):
                continue
            if len(controllers) != 1 or any(
                controllers[0].get(key) != value
                for key, value in {
                    "apiVersion": "batch/v1",
                    "kind": "Job",
                    "name": spec["name"],
                    "uid": uid,
                }.items()
            ):
                raise ValueError("owner changed")
            if pod.get("status", {}).get("phase") != "Succeeded":
                continue
            actual = pod["spec"]
            labels = metadata.get("labels", {})
            containers = actual.get("containers", [])
            statuses = pod["status"].get("containerStatuses", [])
            expected = workspace.objects(operation, target, plan)[0]["spec"][
                "template"
            ]["spec"]
            if (
                metadata.get("namespace") != target["namespace"]
                or not metadata.get("uid")
                or not metadata.get("name")
                or metadata.get("deletionTimestamp") is not None
                or labels.get("superplane.ai/capacity") != plan.cluster_name
                or labels.get("superplane.io/workspace")
                != operation.grant.lease.workspace_id
                or any(
                    actual.get(key, False) is not False
                    for key in ("hostNetwork", "hostPID", "hostIPC")
                )
                or actual.get("dnsPolicy", "ClusterFirst") != "ClusterFirst"
                or not isinstance(actual.get("nodeName"), str)
                or not actual["nodeName"]
                or actual.get("nodeSelector") != expected["nodeSelector"]
                or any(
                    t not in actual.get("tolerations", [])
                    for t in expected["tolerations"]
                )
                or len(containers) != 1
                or any(
                    containers[0].get(key, [] if key == "args" else None) != value
                    for key, value in {
                        "name": "workload",
                        "image": spec["image"],
                        "command": spec["command"],
                        "args": spec["args"],
                    }.items()
                )
                or not _resources_equal(
                    containers[0].get("resources"),
                    expected["containers"][0]["resources"],
                )
                or len(statuses) != 1
                or statuses[0].get("name") != "workload"
                or type(
                    statuses[0].get("state", {}).get("terminated", {}).get("exitCode")
                )
                is not int
                or statuses[0]["state"]["terminated"]["exitCode"] != 0
            ):
                raise ValueError("Pod invocation or placement changed")
            ipaddress.ip_address(pod["status"]["podIP"])
            completed.append(pod)
    except (KeyError, TypeError, AttributeError, ValueError):
        raise OperationRefused("completed ordinary Pod evidence invalid") from None
    if len(completed) != 1:
        if completed:
            raise OperationRefused("completed Pod identity is ambiguous")
        return False
    pod = completed[0]
    for kind, name, original in (
        ("Pod", pod["metadata"]["name"], pod),
        ("Job", spec["name"], job),
    ):
        current = await workspace.request(
            operation, target, "GET", workspace.path(target, kind, name)
        )
        if current.status_code != 200 or current.json() != original:
            raise OperationRefused("completed workload changed during observation")
    await authorize()
    return True
