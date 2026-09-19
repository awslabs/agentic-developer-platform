"""Derive the read-only plan Pod from the migration Job — Issue #5042 (U3).

## Why this exists as a script rather than a few lines of YAML

The plan must run under EXACTLY the conditions the upgrade will run under — same image
digest, same ServiceAccount, same `search_path`, same version table — or it is a plan for a
different situation than the one being planned. A hand-written second manifest would be a
second copy of all of that, free to drift from the Job in ways that make the plan reassuring
and wrong.

So the Pod is DERIVED: everything comes from the rendered Job, and only the argument vector
changes. `alembic current` and `alembic history` are read-only.

## Why not `kubectl run --overrides=...`

That was the first version. It required building JSON inside a `$(…)` inside a `--overrides=`
argument inside a `run:` block — four layers of quoting around a value, in a workflow whose
whole point is that values are not source code. It also could not be tested without a
cluster. Emitting a manifest means the plan step is `kubectl apply --dry-run` /
`kubectl apply` on a file, and this transformation is testable directly.

## The one thing this refuses

If the Job's environment does not carry the schema boundary, no plan Pod is emitted. A plan
run outside the boundary would connect with a `search_path` that could resolve into another
module's schema — a read, but a read that reports another tenant's revision state as this
chain's.
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

import yaml

# Read-only. `current` reports the revision the database is at; `history` reports the chain.
# Neither writes, and neither is `upgrade`.
PLAN_ARGS = ["-c", "/app/src/superplane-api/alembic.ini", "current", "--verbose"]

REQUIRED_ENV = ("SUPERPLANE_DB_CONNECTION_URI", "PGOPTIONS", "ALEMBIC_VERSION_TABLE")


def plan_pod(job: dict, *, name_suffix: str) -> dict:
    if job.get("kind") != "Job":
        raise ValueError(f"expected a Job, got {job.get('kind')!r}")

    template = ((job.get("spec") or {}).get("template")) or {}
    pod_spec = copy.deepcopy(template.get("spec") or {})
    containers = pod_spec.get("containers") or []
    if len(containers) != 1:
        raise ValueError(
            f"expected exactly one container in the Job, found {len(containers)}"
        )

    container = containers[0]
    declared = {entry.get("name") for entry in container.get("env") or []}
    missing = [name for name in REQUIRED_ENV if name not in declared]
    if missing:
        raise ValueError(
            f"the Job's container declares no {', '.join(missing)}, so a plan Pod derived from "
            f"it would read the chain state outside the schema boundary — reporting another "
            f"schema's revision state as this chain's. Refusing to emit a plan Pod."
        )

    if not container.get("image"):
        raise ValueError(
            "the Job's container has no image; a plan must run the pinned runner"
        )

    # Only the arguments change. The entrypoint stays an argv vector, not a shell.
    container["args"] = list(PLAN_ARGS)
    container["name"] = "migrate-plan"
    pod_spec["restartPolicy"] = "Never"

    job_metadata = job.get("metadata") or {}
    labels = dict((template.get("metadata") or {}).get("labels") or {})
    labels["app.kubernetes.io/component"] = "migrate-plan"

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": f"superplane-migrate-plan-{name_suffix}",
            "namespace": job_metadata.get("namespace"),
            "labels": labels,
        },
        "spec": pod_spec,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--name-suffix",
        required=True,
        help="Distinguishes concurrent plan Pods; the run id, so the object records its origin.",
    )
    args = parser.parse_args(argv)

    if not args.name_suffix.strip():
        print(
            "::error::--name-suffix must not be empty; it becomes part of the Pod name"
        )
        return 1

    try:
        job = yaml.safe_load(args.job_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        print(f"::error::could not read the rendered Job at {args.job_file}: {exc}")
        return 1
    if not isinstance(job, dict):
        print(f"::error::{args.job_file} did not parse as a single Job document")
        return 1

    try:
        pod = plan_pod(job, name_suffix=args.name_suffix)
    except ValueError as exc:
        print(f"::error::{exc}")
        return 1

    args.output.write_text(yaml.safe_dump(pod, sort_keys=False), encoding="utf-8")
    print(
        f"Plan Pod written to {args.output} (args: {' '.join(PLAN_ARGS)}) — read-only."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
