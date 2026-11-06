#!/usr/bin/env python3
"""Evaluate private, read-only gateway cooldown evidence without declaring live acceptance."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Cooldown timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def configuration(snapshot):
    objects = {}
    for item in snapshot["items"]:
        kind = item.get("kind")
        name = item.get("metadata", {}).get("name")
        if (kind, name) in (("Deployment", "bedrockgateway"), ("HorizontalPodAutoscaler", "bedrockgateway"),
                            ("PodDisruptionBudget", "bedrockgateway-pdb")):
            if kind in objects:
                raise ValueError("Duplicate gateway configuration in private snapshot")
            objects[kind] = item
    if len(objects) != 3:
        raise ValueError("Gateway snapshot needs Deployment, HPA and PDB")
    if any(item["metadata"].get("namespace") != "adp-gateway" or not item["metadata"].get("uid")
           for item in objects.values()):
        raise ValueError("Gateway snapshot needs a bound namespace and object UIDs")
    deployment = objects["Deployment"]
    return {
        "deployment.template": deployment["spec"]["template"],
        "deployment.strategy": deployment["spec"]["strategy"],
        "deployment.uid": deployment["metadata"].get("uid"),
        "hpa.spec": objects["HorizontalPodAutoscaler"]["spec"],
        "hpa.uid": objects["HorizontalPodAutoscaler"]["metadata"].get("uid"),
        "pdb.spec": objects["PodDisruptionBudget"]["spec"],
        "pdb.uid": objects["PodDisruptionBudget"]["metadata"].get("uid"),
    }


def baseline(sample):
    replicas = sample["replicas"]
    pods = sample["pods"]
    return (all(replicas.get(name) == 2 for name in
                ("desired", "current", "ready", "hpa_desired", "hpa_current"))
            and len(pods) == 2
            and all(pod.get("ready") and pod.get("endpoint_ready") is True
                    and pod.get("endpoint_serving") is True for pod in pods))


def evaluate(samples, before, after, stopped_at, deadline_seconds):
    if not 1 <= deadline_seconds <= 600:
        raise ValueError("Cooldown observation deadline must be between 1 and 600 seconds")
    stopped = timestamp(stopped_at)
    deadline = stopped + timedelta(seconds=deadline_seconds)
    previous = configuration(before)
    current = configuration(after)
    changes = [name for name in previous if previous[name] != current[name]]
    if current["hpa.spec"].get("minReplicas") != 2:
        changes.append("hpa.minReplicas")
    timeline = []
    for sample in samples:
        observed = timestamp(sample["observed_at"])
        if observed < stopped or (timeline and observed <= timestamp(timeline[-1]["observed_at"])):
            raise ValueError("Cooldown samples must follow the admissions stop in timestamp order")
        pods = sample["pods"]
        timeline.append({
            "observed_at": sample["observed_at"], "replicas": sample["replicas"],
            "metrics_available": sample["metrics_available"],
            "ready_baseline": baseline(sample),
            "endpoint_healthy": all(pod.get("endpoint_ready") is True and pod.get("endpoint_serving") is True
                                    for pod in pods if pod.get("ready")),
            "restarts_unknown_or_nonzero": any(pod.get("restarts") != 0 for pod in pods),
        })
    eligible = [item for item in timeline if timestamp(item["observed_at"]) <= deadline]
    returned = len(eligible) >= 2 and all(item["ready_baseline"] for item in eligible[-2:])
    metrics_ok = bool(timeline) and all(item["metrics_available"] for item in timeline)
    restarts_ok = bool(timeline) and not any(item["restarts_unknown_or_nonzero"] for item in timeline)
    endpoints_ok = bool(timeline) and all(item["endpoint_healthy"] for item in timeline)
    reasons = []
    if changes:
        reasons.append("configuration_changed")
    if not metrics_ok:
        reasons.append("metrics_unavailable")
    if not restarts_ok:
        reasons.append("restart_or_unknown")
    if not endpoints_ok:
        reasons.append("endpoint_unhealthy")
    if not returned:
        reasons.append("two_ready_replicas_not_observed_within_deadline")
    return {
        "status": "candidate" if not reasons else "incomplete",
        "reasons": reasons, "admissions_stopped_at": stopped_at,
        "deadline_at": deadline.isoformat(), "configuration_changes": changes,
        "timeline": timeline, "manual_health_and_no_forced_scaling_receipts_required": True,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=Path, required=True, help="private sampler JSONL")
    parser.add_argument("--before", type=Path, required=True, help="private pre-stop Kubernetes List snapshot")
    parser.add_argument("--after", type=Path, required=True, help="private post-observation Kubernetes List snapshot")
    parser.add_argument("--admissions-stopped-at", required=True, help="operator-attested UTC stop timestamp")
    parser.add_argument("--deadline-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path, required=True, help="new private receipt path")
    arguments = parser.parse_args()
    try:
        if arguments.output.exists():
            raise ValueError("Cooldown receipt already exists; refusing overwrite")
        samples = [json.loads(line) for line in arguments.samples.read_text().splitlines() if line.strip()]
        if len(samples) > 1440:
            raise ValueError("Cooldown sample count exceeds the sampler limit")
        result = evaluate(samples, json.loads(arguments.before.read_text()), json.loads(arguments.after.read_text()),
                          arguments.admissions_stopped_at, arguments.deadline_seconds)
        descriptor = os.open(arguments.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(result, output)
            output.write("\n")
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, f"Cooldown evidence incomplete: {type(error).__name__}; retain private inputs for review\n")


if __name__ == "__main__":
    main()
