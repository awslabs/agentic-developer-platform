"""Opt-in approved batch invocation for an existing ordinary ClusterIP Service."""

import hashlib
import ipaddress
import json
import re
import uuid

from harness_jobs.identity import OperationRefused

COMMAND = ["python3", "-m", "superplane_executor.network_workload_probe"]
PROFILE_FIELDS = {
    "version",
    "service_name",
    "namespace",
    "service_uid",
    "port",
    "cidrs",
}
BINDING_FIELDS = {
    "org_id",
    "workspace_id",
    "allocation_id",
    "cluster_arn",
    "nonce",
    "request_id",
}


def nonce(org_id, workspace_id, request_id):
    return hashlib.sha256(
        json.dumps(
            ["superplane-network-probe:v1", org_id, workspace_id, str(request_id)],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def validate_service_spec(value, namespace, service_cidr):
    if (
        set(value) != PROFILE_FIELDS
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("unsupported network probe profile")
    if (
        value["namespace"] != namespace
        or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value["service_name"]
        )
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace)
        or not re.fullmatch(r"[a-zA-Z0-9-]{1,128}", value["service_uid"])
        or type(value["port"]) is not int
        or not 1 <= value["port"] <= 65535
        or not isinstance(value["cidrs"], list)
        or not 1 <= len(value["cidrs"]) <= 4
        or len(set(value["cidrs"])) != len(value["cidrs"])
    ):
        raise ValueError("invalid network probe Service identity")
    approved = ipaddress.ip_network(service_cidr, strict=True)
    if any(
        not ipaddress.ip_network(c, strict=True).subnet_of(approved)
        for c in value["cidrs"]
    ):
        raise ValueError(
            "probe addresses must remain within the bound cluster Service CIDR"
        )


def invocation(profile, *, org_id, workspace_id, request_id, allocation_id, target):
    descriptor = profile["network_probe"]
    validate_service_spec(descriptor, target["namespace"], profile["service_cidr"])
    workload = profile["workload"]
    if (
        workload["kind"] != "batch"
        or workload["command"] != COMMAND
        or workload["args"] != []
    ):
        raise ValueError("network probe requires the explicit installed batch command")
    contract = {
        **descriptor,
        "org_id": org_id,
        "workspace_id": workspace_id,
        "allocation_id": allocation_id,
        "cluster_arn": target["cluster_arn"],
        "nonce": nonce(org_id, workspace_id, request_id),
        "request_id": str(request_id),
    }
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    if len(encoded) > 1024:
        raise ValueError("network probe invocation exceeds approved argument bound")
    return [encoded]


def read(data, *, org_id, workspace_id, request_id, allocation_id):
    workload = data["workload"]
    if workload["command"] != COMMAND:
        return None
    try:
        if workload["kind"] != "batch" or len(workload["args"]) != 1:
            raise ValueError("invalid probe invocation")
        value = json.loads(workload["args"][0])
        if set(value) != PROFILE_FIELDS | BINDING_FIELDS:
            raise ValueError("invalid probe contract")
        validate_service_spec(
            {k: value[k] for k in PROFILE_FIELDS},
            data["namespace"],
            data["service_cidr"],
        )
        original_request = str(uuid.UUID(value["request_id"]))
        if value["request_id"] != original_request or (
            request_id is not None and original_request != str(request_id)
        ):
            raise ValueError("probe request binding differs")
        expected = {
            "org_id": org_id,
            "workspace_id": workspace_id,
            "allocation_id": allocation_id,
            "cluster_arn": data["cluster_arn"],
            "nonce": nonce(org_id, workspace_id, original_request),
        }
        if any(value[k] != v for k, v in expected.items()):
            raise ValueError("network probe approval binding differs")
        return value
    except (ValueError, KeyError, TypeError, AttributeError):
        raise OperationRefused("approved network probe contract invalid") from None


def for_operation(operation, plan):
    if plan.data["workload"]["command"] != COMMAND:
        return None
    return read(
        plan.data,
        org_id=operation.grant.lease.org_id,
        workspace_id=operation.grant.lease.workspace_id,
        request_id=operation.request.idempotency_key
        if operation.request.action == "provision"
        else None,
        allocation_id=operation.request.parameters["allocation_id"],
    )


def endpoint(contract):
    return f"http://{contract['service_name']}.{contract['namespace']}.svc.cluster.local:{contract['port']}/"


async def service(workspace, operation, target, contract):
    response = await workspace.request(
        operation,
        target,
        "GET",
        workspace.path(target, "Service", contract["service_name"]),
    )
    if response.status_code != 200:
        raise OperationRefused("approved probe Service unavailable")
    obj = response.json()
    try:
        metadata, spec = obj["metadata"], obj["spec"]
        ranges = [ipaddress.ip_network(c, strict=True) for c in contract["cidrs"]]
        addresses = spec.get("clusterIPs", [spec["clusterIP"]])
        if (
            metadata["uid"] != contract["service_uid"]
            or metadata["name"] != contract["service_name"]
            or metadata["namespace"] != target["namespace"]
            or metadata.get("deletionTimestamp") is not None
            or spec.get("type", "ClusterIP") != "ClusterIP"
            or not addresses
            or any(
                not any(ipaddress.ip_address(a) in network for network in ranges)
                for a in addresses
            )
            or not any(
                p.get("port") == contract["port"] and p.get("protocol", "TCP") == "TCP"
                for p in spec["ports"]
            )
        ):
            raise ValueError("Service changed")
        return obj
    except (ValueError, KeyError, TypeError, AttributeError):
        raise OperationRefused(
            "approved probe Service identity or address differs"
        ) from None


def closed_execution(pod, expected):
    """An image digest does not pin code/resolvers hidden by injected mounts.

    The generated batch template already disables API-token automount. This
    probe has no volume, environment, sidecar or hook contract; none may be
    introduced by admission and then used as evidence of the approved code.
    Kubernetes defaults unrelated to execution (e.g. scheduling metadata and
    imagePullSecrets) do not need an exception to these checks.
    """
    try:
        actual = pod["spec"]
        containers = actual["containers"]
        if (
            actual.get("automountServiceAccountToken") is not False
            or any(
                actual.get(key, []) != []
                for key in ("volumes", "initContainers", "ephemeralContainers")
            )
            or any(
                actual.get(key, False) is not False
                for key in (
                    "hostNetwork",
                    "hostPID",
                    "hostIPC",
                    "shareProcessNamespace",
                )
            )
            or actual.get("runtimeClassName") is not None
            or actual.get("securityContext") != expected["securityContext"]
            or actual.get("restartPolicy") != "Never"
            or len(containers) != 1
        ):
            raise ValueError("probe Pod execution surface changed")
        container, original = containers[0], expected["containers"][0]
        if (
            any(
                container.get(key, [] if key == "args" else None) != original[key]
                for key in ("name", "image", "command", "args", "securityContext")
            )
            or any(
                container.get(key, []) != []
                for key in ("env", "envFrom", "volumeMounts", "volumeDevices")
            )
            or any(
                container.get(key) is not None
                for key in (
                    "workingDir",
                    "lifecycle",
                    "livenessProbe",
                    "readinessProbe",
                    "startupProbe",
                )
            )
            or any(
                container.get(key, False) is not False
                for key in ("stdin", "stdinOnce", "tty")
            )
            or container.get("terminationMessagePath", "/dev/termination-log")
            != "/dev/termination-log"
            or container.get("terminationMessagePolicy", "File") != "File"
        ):
            raise ValueError("probe container execution surface changed")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise OperationRefused("approved probe execution surface changed") from None


async def verify_result(
    workspace, operation, target, plan, contract, pod, content, authorize
):
    from .network_observation import pod_service

    expected = workspace.objects(operation, target, plan)[0]["spec"]["template"]["spec"]
    closed_execution(pod, expected)
    await authorize()
    original = await service(workspace, operation, target, contract)
    observation = await pod_service(
        workspace,
        operation,
        target,
        pod_name=pod["metadata"]["name"],
        pod_uid=pod["metadata"]["uid"],
        node_name=pod["spec"].get("nodeName"),
        nonce=contract["nonce"],
        endpoint=endpoint(contract),
        cidrs=contract["cidrs"],
        allocation_label=plan.cluster_name,
        validate_pod=lambda current: closed_execution(current, expected),
    )
    try:
        retained = json.loads(content)
        addresses = original["spec"].get("clusterIPs", [original["spec"]["clusterIP"]])
        if retained != observation["pod_service"] or set(retained["addresses"]) != set(
            addresses
        ):
            raise ValueError("receipt differs")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise OperationRefused(
            "approved probe log, result or Service addresses differ"
        ) from None
    if await service(workspace, operation, target, contract) != original:
        raise OperationRefused("approved probe Service changed during observation")
    await authorize()
