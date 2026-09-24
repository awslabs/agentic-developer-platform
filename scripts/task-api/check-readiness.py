#!/usr/bin/env python3
"""Validate a captured rollout inventory; never enable admission or mutate AWS."""

import argparse
import hashlib
import json
import re
from pathlib import Path

DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
FLAGS = (
    "ADP_TASK_API_READ_ENABLED",
    "ADP_TASK_API_SUBMIT_ENABLED",
    "ADP_TASK_API_WORKER_ENABLED",
    "ADP_TASK_API_RECOVERY_ENABLED",
)


def inspect(inventory, directory):
    failures = []
    required = (
        "environment",
        "account_id",
        "region",
        "captured_at",
        "source_sha",
        "queue_arn",
        "gateway_version",
        "producer_version",
        "worker_digest",
        "queue_consumers",
        "flags",
        "evidence",
        "policy",
    )
    failures.extend(
        "missing " + field for field in required if not inventory.get(field)
    )
    digest = inventory.get("worker_digest") or ""
    if not DIGEST.fullmatch(digest):
        failures.append("worker_digest must be immutable sha256")
    consumers = inventory.get("queue_consumers", [])
    if not consumers:
        failures.append("no actual queue consumer inventory")
    for consumer in consumers:
        if consumer.get("queue_arn") != inventory.get("queue_arn"):
            failures.append("queue identity mismatch")
        if (
            consumer.get("image_digest") != digest
            or consumer.get("task_capable") is not True
        ):
            failures.append(
                "incompatible or unverified consumer: " + consumer.get("name", "?")
            )
        if consumer.get("source") not in (
            "pod",
            "job",
            "deployment",
            "scaledjob",
            "lambda-event-source",
        ):
            failures.append("unknown consumer source")
    sources = {item.get("source") for item in consumers}
    if (
        not {"deployment", "scaledjob"}.issubset(sources)
        or inventory.get("job_templates_enumerated") is not True
    ):
        failures.append(
            "inventory must cover standing workers, retained jobs and future scaled jobs"
        )
    if inventory.get("old_jobs_drained_or_proven_nonrestartable") is not True:
        failures.append("old shared-queue jobs may restart incompatible consumers")
    if (
        inventory.get("task_projected_token_audience") != "adp-agent-bootstrap"
        or inventory.get("task_service_account") != "agent-scaledjob-sa"
    ):
        failures.append(
            "task-only projected workload token and approved service account required"
        )
    if inventory.get("all_queue_consumers_enumerated") is not True:
        failures.append("queue consumer enumeration incomplete")
    flags = inventory.get("flags", {})
    if any(type(flags.get(flag)) is not bool for flag in FLAGS):
        failures.append("all four canonical task flags require observed boolean values")
    if flags.get("ADP_RUN_TASKS_ENABLED") is not False:
        failures.append("legacy generic task execution must remain off")
    if flags.get("ADP_AGENT_AUTHORITY_ENABLED") not in (None, False):
        failures.append("generic agent authority must remain false or absent")
    if inventory.get("task_workload_token_file") != "/var/run/adp-workload/token":
        failures.append("task workload token file binding is missing")
    policy = inventory.get("policy", {})
    if (
        not policy.get("principal_id")
        or policy.get("persona") != "agent-task-investigator"
    ):
        failures.append("canonical principal and allowed persona required")
    if (
        not 0 < policy.get("max_tasks", 0) <= 3
        or not 0 < policy.get("max_total_usd", 0) <= 3
    ):
        failures.append(
            "evidence run requires at most3 tasks and USD3 explicit spend cap"
        )
    if not policy.get("expires_at"):
        failures.append("evidence policy expiry required")
    evidence = inventory.get("evidence", {})
    for name in (
        "workers",
        "scalers",
        "lambda",
        "queue",
        "flags",
        "capability",
        "principal",
        "legacy_baseline",
    ):
        item = evidence.get(name, {})
        relative = item.get("path")
        path = (directory / relative).resolve() if isinstance(relative, str) else None
        if (
            path is None
            or not path.is_relative_to(directory.resolve())
            or not path.is_file()
        ):
            failures.append("missing captured evidence: " + name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != item.get("sha256"):
            failures.append("evidence hash mismatch: " + name)
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    inventory = json.loads(args.inventory.read_text())
    failures = inspect(inventory, args.inventory.parent)
    report = {
        "schema_version": "1.0",
        "lane": "inventory-only",
        "outcome": "BLOCKED" if failures else "READY FOR OPERATOR REVIEW",
        "live_acceptance": "NOT RUN",
        "admission_changed": False,
        "source_sha": inventory.get("source_sha"),
        "failures": failures,
        "inventory_sha256": hashlib.sha256(args.inventory.read_bytes()).hexdigest(),
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
