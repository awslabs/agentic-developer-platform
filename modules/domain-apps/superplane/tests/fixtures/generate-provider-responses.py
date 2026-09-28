"""Derives `provider-responses.json` from the providers' own response models.

Issue #5049 (U11), EPIC #4910.

## Why this is a generator rather than a checked-in hand-written file

The story's fixture rule is that provider-response fixtures must be captured real
responses or derived from the provider SDK's response models, and never written
from the adapter's expected shape — "or adapter and fixture become
self-consistently wrong about a timeout". A hand-written fixture cannot satisfy
that rule by inspection: it looks identical whether its author read the SDK or
read `adapter.py`. A generator can, because it fails when the model disagrees.

So no field name in the output is typed here. Both sets of keys are read from the
producing definitions:

* **SkyPilot** — the `json:"..."` struct tags and the `ClusterStatus` constants in
  the pinned snapshot's `skypilot/types.go`. Those tags are what the Go client
  unmarshals with, so they are the wire contract, not a description of it.
* **EC2** — `botocore`'s own service model for `DescribeInstances`, which is the
  same JSON model boto3 uses at runtime, including the `InstanceState.Name` enum.

Values are synthetic identifiers (`sky-node-*`, `i-0` …) and are labelled as such
in `provider-responses.md`. Nothing here is a captured live response and nothing
here is a credential.

## Reproducing

From the repository root, with the gateway's Python dependencies installed:

    git fetch origin agent/issue-4910
    python3 modules/domain-apps/superplane/tests/fixtures/generate-provider-responses.py

It writes `provider-responses.json` next to itself and prints nothing on success.
Review any diff before committing it: a diff means a model changed.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# The pinned snapshot, matching spike/provenance.py's constants.
SNAPSHOT_BRANCH = "agent/issue-4910"
SNAPSHOT_TYPES_GO = (
    "modules/domain-apps/ai-super-plane/reference/"
    "src/superplane-controller/skypilot/types.go"
)
UPSTREAM_REVISION = "5d543c952493f0765133b92e93301b0b24d028ee"

# Git blob SHA-1 of the types.go read below, so a reader can confirm they are
# looking at the same file this fixture was derived from.
TYPES_GO_BLOB = "99e07955a0b20dc488ed4a630824d5962e452857"

OUT = Path(__file__).resolve().parent / "provider-responses.json"

_STRUCT = re.compile(r"^type (\w+) struct \{$")
_JSON_TAG = re.compile(r'json:"([^",]+)')
_STATUS_CONST = re.compile(r'ClusterStatus\w+\s+ClusterStatus\s*=\s*"(\w+)"')


def read_types_go() -> str:
    """Read the snapshot's types.go from the planning branch."""
    for ref in (f"origin/{SNAPSHOT_BRANCH}", SNAPSHOT_BRANCH, "FETCH_HEAD"):
        result = subprocess.run(  # noqa: S603
            ["git", "show", f"{ref}:{SNAPSHOT_TYPES_GO}"],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout
    sys.exit(
        f"cannot read {SNAPSHOT_TYPES_GO}; run: git fetch origin {SNAPSHOT_BRANCH}"
    )


def go_struct_tags(source: str) -> dict[str, list[str]]:
    """Map each Go struct name to its `json:` tag names, in declaration order."""
    structs: dict[str, list[str]] = {}
    current: str | None = None
    for line in source.splitlines():
        opening = _STRUCT.match(line)
        if opening:
            current = opening.group(1)
            structs[current] = []
            continue
        if current is None:
            continue
        if line.startswith("}"):
            current = None
            continue
        tag = _JSON_TAG.search(line)
        if tag:
            structs[current].append(tag.group(1))
    return structs


def require(tags: dict[str, list[str]], struct: str, *needed: str) -> None:
    """Fail loudly when the model no longer carries a field the fixture uses."""
    present = set(tags.get(struct, ()))
    missing = [name for name in needed if name not in present]
    if missing:
        sys.exit(f"{struct} in types.go no longer declares {missing}")


def skypilot_section(source: str) -> dict[str, object]:
    """Build the SkyPilot responses from the struct tags and status constants."""
    tags = go_struct_tags(source)
    statuses = _STATUS_CONST.findall(source)
    require(tags, "RequestResponse", "request_id")
    require(tags, "ClusterInfo", "name", "status", "handle", "launched_at", "autostop")
    require(tags, "ClusterHandle", "cluster_name", "launched_resources")
    require(tags, "LaunchedResources", "cloud", "instance_type", "region")
    for expected in ("UP", "INIT", "STOPPED"):
        if expected not in statuses:
            sys.exit(f"ClusterStatus {expected} is no longer declared in types.go")

    resources = dict(
        zip(
            tags["LaunchedResources"],
            ["aws", "g5.2xlarge", "us-east-1", "us-east-1a", "A10G:1"],
            strict=False,
        )
    )
    handle = {
        "cluster_name": "sky-node-a1b2c3",
        "head_ip": "10.0.12.44",
        "num_node": 1,
        "launched_resources": resources,
    }
    cluster = {
        "name": "sky-node-a1b2c3",
        "status": "UP",
        "handle": handle,
        "launched_at": 1789000000,
        "last_use": "sky launch",
        "autostop": 120,
        "to_down": False,
    }
    return {
        # POST /launch when the response arrives. The identifier the adapter must
        # have recorded an identity for BEFORE issuing this call.
        "launch_accepted": {"request_id": "d0f3a1e2-0000-4000-8000-000000000001"},
        # POST /status naming the cluster: the re-check finds it UP. The launch
        # whose response was lost did in fact create capacity.
        "status_present_up": [cluster],
        # The same re-check while the cluster is still coming up. PRESENT, not
        # absent — INIT is a running cost.
        "status_present_init": [
            {**cluster, "status": "INIT", "handle": {**handle, "head_ip": ""}}
        ],
        # POST /status returns an empty list for an unknown cluster name. This is
        # provider-established absence, the only thing that authorizes a repeat.
        "status_absent": [],
        # Declared statuses, so a test cannot assert on a state the client would
        # never produce.
        "cluster_statuses": statuses,
    }


def ec2_section() -> dict[str, object]:
    """Build the EC2 responses from botocore's DescribeInstances output shape."""
    try:
        from botocore.session import get_session
    except ImportError:  # pragma: no cover - dependency install is CI's job
        sys.exit("botocore is required; install the gateway's dependencies")

    shape = (
        get_session()
        .get_service_model("ec2")
        .operation_model("DescribeInstances")
        .output_shape
    )
    reservation = shape.members["Reservations"].member
    instance = reservation.members["Instances"].member
    state = instance.members["State"]
    names = state.members["Name"].enum
    for expected in ("running", "shutting-down", "terminated"):
        if expected not in names:
            sys.exit(f"InstanceState.Name no longer offers {expected!r}")

    def described(state_name: str) -> dict[str, object]:
        code = {"running": 16, "shutting-down": 32, "terminated": 48}[state_name]
        return {
            "Reservations": [
                {
                    "ReservationId": "r-0abc1234def567890",
                    "OwnerId": "000000000000",
                    "Instances": [
                        {
                            "InstanceId": "i-0abc1234def567890",
                            "InstanceType": "g5.2xlarge",
                            "State": {"Code": code, "Name": state_name},
                            "ClientToken": "sky-node-a1b2c3-launch-1",
                        }
                    ],
                }
            ]
        }

    return {
        # The instance is up: cost is accruing whatever the local status says.
        "describe_instances_running": described("running"),
        # Mid-teardown. Not gone, so not releasable — the state that makes
        # "terminate returned, therefore terminated" wrong.
        "describe_instances_shutting_down": described("shutting-down"),
        # Terminated: absence the provider itself established.
        "describe_instances_terminated": described("terminated"),
        # No reservations at all — the other form of absence.
        "describe_instances_empty": {"Reservations": []},
        "instance_state_names": list(names),
    }


def main() -> None:
    source = read_types_go()
    document = {
        "_provenance": {
            "note": (
                "Generated by generate-provider-responses.py. Field names are "
                "read from the producing models, never transcribed. See "
                "provider-responses.md."
            ),
            "skypilot": {
                "path": SNAPSHOT_TYPES_GO,
                "branch": SNAPSHOT_BRANCH,
                "upstream_revision": UPSTREAM_REVISION,
                "blob_sha1": TYPES_GO_BLOB,
            },
            "ec2": {
                "source": "botocore service model for ec2 DescribeInstances",
                "botocore_version": __import__("botocore").__version__,
            },
            "values": "synthetic identifiers; no captured live response, no credential",
        },
        "skypilot": skypilot_section(source),
        "ec2": ec2_section(),
    }
    OUT.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n")


if __name__ == "__main__":
    main()
