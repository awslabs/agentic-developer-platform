"""Discover separately owned Superplane state before core platform teardown.

Discovery is read-only. An installation reset must remove these resources through
their app-owned lifecycle entrypoints before removing their management dependencies.
An unfamiliar state is a blocker, never permission to discard it or delete by prefix.
"""

import hashlib
import json
from pathlib import Path

# These core phases remove the app or identities, storage and cluster access used
# by its separate state owners. Discovery does not add those owners to a destroy.
DEPENDENCY_PHASES = frozenset(("superplane", "webhook_ingress", "gateway", "platform"))


def discover(*, call, account, bucket, environment, directory):
    prefix = f"{environment}/modules/superplane-"
    listing = call(
        "s3api",
        "list-objects-v2",
        "--bucket",
        bucket,
        "--expected-bucket-owner",
        account,
        "--prefix",
        prefix,
    )
    states = {}
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    for item in listing.get("Contents", []):
        key = item["Key"]
        # Check the response as well as the request; never follow an unexpected
        # object into another environment or treat a backup as current state.
        if not key.startswith(prefix) or not key.endswith(".tfstate"):
            continue
        path = directory / (hashlib.sha256(key.encode()).hexdigest() + ".tfstate")
        result = call(
            "s3api",
            "get-object",
            "--bucket",
            bucket,
            "--key",
            key,
            "--expected-bucket-owner",
            account,
            str(path),
            absent=("NoSuchKey",),
        )
        if result is None:
            raise ValueError("Superplane state disappeared during discovery: " + key)
        path.chmod(0o600)
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or not isinstance(state.get("resources"), list):
            raise TypeError("Invalid Superplane state: " + key)
        # Empty retired states and data sources do not own cloud resources.
        count = sum(
            len(resource.get("instances", []))
            for resource in state["resources"]
            if resource.get("mode") == "managed"
        )
        if count:
            states[key] = {
                "lineage": state.get("lineage"),
                "resource_count": count,
                "state_file": str(path),
            }
    return states


def blockers(states, phases):
    return sorted(states) if DEPENDENCY_PHASES.intersection(phases) else []
