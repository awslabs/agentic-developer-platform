"""Approval-bound graph headers; compilation is independent of SQL and providers."""

import hashlib
import json
import re

from harness_jobs.execution_descriptors import ExecutionStep, encode_execution_steps
from harness_jobs.identity import MAX_PARAMETER_VALUE_LENGTH, OperationRefused

PARAMETER = "controller_cleanup_graph"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read(raw):
    try:
        value = json.loads(raw)
        if (
            len(raw) > MAX_PARAMETER_VALUE_LENGTH
            or set(value)
            != {
                "version",
                "snapshot_id",
                "snapshot_sha256",
                "nodes",
                "roots",
                "network",
            }
            or type(value["version"]) is not int
            or value["version"] != 1
            or any(
                not isinstance(value[k], str)
                or not re.fullmatch(r"[a-f0-9]{64}", value[k])
                for k in ("snapshot_id", "snapshot_sha256")
            )
            or any(
                type(value[k]) is not int or not 0 <= value[k] <= 64
                for k in ("nodes", "roots", "network")
            )
            or raw != canonical(value)
        ):
            raise ValueError()
        if value["nodes"] > 16 or value["roots"] > 2:
            raise ValueError()
        return value
    except (ValueError, TypeError, KeyError):
        raise OperationRefused("cleanup graph header is invalid") from None


def steps(header, target):
    header = read(canonical(header))
    stages = [f"cordon:{i}" for i in range(header["nodes"])]
    stages += [f"root:{i}" for i in range(header["roots"])]
    stages += ["drain", "down"]
    stages += [f"node:{i}" for i in range(header["nodes"])]
    stages += [f"network:{i}" for i in range(header["network"])]
    stages += ["inventory"]
    # The shared encoder enforces existing step/byte bounds, without truncation.
    return encode_execution_steps(
        tuple(ExecutionStep(stage, "aws", "delete_cluster", target) for stage in stages)
    )


def header(document):
    body_digest = digest(document)
    value = {
        "version": 1,
        "snapshot_id": digest([document["source_operation_id"], body_digest]),
        "snapshot_sha256": body_digest,
        "nodes": len(document["nodes"]),
        "roots": len(document["roots"]),
        "network": len(document["network"]),
    }
    steps(value, document["cluster_name"])
    return value
