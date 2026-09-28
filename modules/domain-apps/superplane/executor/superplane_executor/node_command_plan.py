"""Closed opt-in native bootstrap transport, bound by the original approval."""

import hashlib
import json
import re

from harness_jobs.identity import MAX_PARAMETER_VALUE_LENGTH, OperationRefused

PARAMETER = "controller_node_bootstrap"
KINDS = {"run-node-bootstrap": "node-bootstrap", "run-node-probe": "node-api-dns-tls"}
DOCUMENTS = {
    "node-bootstrap": (
        "SuperplaneNativeBootstrapV1",
        "02e65ce48a37bd18b64550493fcbeb1cdbcab7f57e667469e60779989417e7a2",
        "bootstrap",
        360,
    ),
    "node-api-dns-tls": (
        "SuperplaneNodeProbeV1",
        "41756cc3ea53232d1f22bff56ab5de5e07e9e22fd3a9b8ad6ca372723e0842ef",
        "probe",
        120,
    ),
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def validate(value):
    from .node_runner import validate_runtime_manifest

    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "version",
            "runtime_manifest",
            "bootstrap_wrapper_sha256",
            "probe_wrapper_sha256",
        }
        or type(value["version"]) is not int
        or value["version"] != 1
        or len(canonical(value)) > MAX_PARAMETER_VALUE_LENGTH
    ):
        raise ValueError("closed native bootstrap descriptor required")
    validate_runtime_manifest(value["runtime_manifest"])
    for field in ("bootstrap_wrapper_sha256", "probe_wrapper_sha256"):
        if not isinstance(value[field], str) or not re.fullmatch(
            r"[a-f0-9]{64}", value[field]
        ):
            raise ValueError("native wrapper provenance required")
    return value


def read(parameters, data, target):
    if PARAMETER not in parameters:
        return None
    try:
        from .workspace import Workspace

        Workspace.require_dedicated_node_authority(target)
        if data["version"] != 4 or "controller_network_cluster" not in parameters:
            raise ValueError("native bootstrap requires approved regional network")
        raw = parameters[PARAMETER]
        value = validate(json.loads(raw))
        if raw != canonical(value):
            raise ValueError("noncanonical native bootstrap descriptor")
        return value
    except (KeyError, TypeError, ValueError):
        raise OperationRefused("approved native bootstrap descriptor invalid") from None


def actions(action, enabled):
    if action == "teardown":
        return ["delete_cluster"]
    if enabled:
        return [
            "launch",
            "run-node-bootstrap",
            "status",
            "run-node-probe",
            "deploy",
            "status",
        ]
    return ["launch", "status", "deploy", "status"]


def reference(operation_id, org_id, workspace_id, allocation_id, instance_id, purpose):
    return "superplane-node-command:" + digest(
        [org_id, workspace_id, allocation_id, operation_id, instance_id, purpose]
    )
