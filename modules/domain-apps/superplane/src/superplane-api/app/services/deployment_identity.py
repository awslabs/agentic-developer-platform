"""Bind a create-only Kubernetes request to its durable, server-owned operation."""

import hashlib
import json
import re
from copy import deepcopy

OPERATION_ANNOTATION = "superplane.io/create-operation"
REQUEST_ANNOTATION = "superplane.io/create-request"


def has_create_binding(manifest):
    annotations = manifest.get("metadata", {}).get("annotations", {})
    return all(
        isinstance(annotations.get(key), str)
        and re.fullmatch(r"[a-f0-9]{64}", annotations[key])
        for key in (OPERATION_ANNOTATION, REQUEST_ANNOTATION)
    )


def bind_manifest(manifest, *, org_id, workspace_id, operation_id, deployment_id):
    """All identities come from the authorized durable row, never manifest input."""
    material = [str(v) for v in (org_id, workspace_id, operation_id, deployment_id)]
    marker = hashlib.sha256(json.dumps(material).encode()).hexdigest()
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    bound = deepcopy(manifest)
    bound["metadata"].setdefault("annotations", {}).update(
        {OPERATION_ANNOTATION: marker, REQUEST_ANNOTATION: digest}
    )
    return bound


def _contains(observed, desired):
    # Kubernetes defaults fields that are absent in the request. Lists, however,
    # must match exactly: an extra container/env/argument is not a default.
    if isinstance(desired, dict):
        return isinstance(observed, dict) and all(
            key in observed and _contains(observed[key], value)
            for key, value in desired.items()
        )
    if isinstance(desired, list):
        return (
            isinstance(observed, list)
            and len(observed) == len(desired)
            and all(_contains(left, right) for left, right in zip(observed, desired))
        )
    return type(observed) is type(desired) and observed == desired


def matches_create(observed, desired):
    """A same-name object is recoverable only as this unchanged create result.

    An observed generation above one proves the specification changed after
    creation, even if the new fields were absent in our desired manifest. Refuse
    rather than overwriting it or treating an old annotation as current evidence.
    """
    if not isinstance(observed, dict):
        return False
    metadata = observed.get("metadata", {})
    expected = desired.get("metadata", {})
    if not has_create_binding(desired):
        return False
    return (
        isinstance(metadata, dict)
        and isinstance(metadata.get("uid"), str)
        and bool(metadata["uid"])
        and type(metadata.get("generation")) is int
        and metadata["generation"] == 1
        and not metadata.get("deletionTimestamp")
        and observed.get("apiVersion") == desired.get("apiVersion")
        and observed.get("kind") == desired.get("kind")
        and _contains(metadata, expected)
        and _contains(observed.get("spec"), desired.get("spec"))
    )
