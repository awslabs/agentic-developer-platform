#!/usr/bin/env python3
"""Sample gateway scale-out stages without exporting pod specs or credentials."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import time


NAMESPACE = "adp-gateway"
LABEL = "app=bedrockgateway"


def kubectl(*arguments, optional=False):
    result = subprocess.run(
        ["kubectl", "--request-timeout=30s", "-n", NAMESPACE, *arguments],
        text=True, capture_output=True, timeout=45, check=False,
    )
    if result.returncode:
        if optional:
            return None
        raise RuntimeError(f"Cannot collect gateway {arguments[1]}; check target access and health")
    return json.loads(result.stdout)


def condition_time(conditions, name):
    return next((item.get("lastTransitionTime") for item in conditions
                 if item.get("type") == name and item.get("status") == "True"), None)


def event_times(events, pod):
    stages = {"Pulling": None, "Pulled": None}
    for event in events:
        if (event.get("involvedObject", {}).get("uid") != pod.get("metadata", {}).get("uid")
                or event.get("reason") not in stages):
            continue
        reason = event["reason"]
        occurred = event.get("eventTime") or event.get("firstTimestamp")
        if occurred and (stages[reason] is None or occurred < stages[reason]):
            stages[reason] = occurred
    return stages


def summarize(deployment, hpa, pods, events, endpoints, metrics, observed_at, nodes=None):
    endpoint_ready = set()
    endpoint_serving = set()
    for slice_item in endpoints.get("items", []):
        for endpoint in slice_item.get("endpoints", []):
            uid = endpoint.get("targetRef", {}).get("uid")
            if endpoint.get("conditions", {}).get("ready") is True:
                endpoint_ready.add(uid)
            if endpoint.get("conditions", {}).get("serving") is True:
                endpoint_serving.add(uid)

    usage = {}
    if metrics:
        for item in metrics.get("items", []):
            usage[item.get("metadata", {}).get("name")] = [
                {"name": container.get("name"), "cpu": container.get("usage", {}).get("cpu"),
                 "memory": container.get("usage", {}).get("memory")}
                for container in item.get("containers", [])
            ]

    pod_report = []
    for pod in pods.get("items", []):
        metadata = pod.get("metadata", {})
        status = pod.get("status", {})
        uid = metadata.get("uid")
        container = next((item for item in status.get("containerStatuses", [])
                          if item.get("name") == "bedrockgateway"), {})
        pod_report.append({
            "name": metadata.get("name"), "node": pod.get("spec", {}).get("nodeName"),
            "created": metadata.get("creationTimestamp"),
            "scheduled": condition_time(status.get("conditions", []), "PodScheduled"),
            "image": event_times(events.get("items", []), pod),
            "init_finished": [item.get("state", {}).get("terminated", {}).get("finishedAt")
                              for item in status.get("initContainerStatuses", [])],
            "container_started": container.get("state", {}).get("running", {}).get("startedAt"),
            "ready": condition_time(status.get("conditions", []), "Ready"),
            "endpoint_ready": uid in endpoint_ready, "endpoint_serving": uid in endpoint_serving,
            "first_request_served": None,
            "restarts": container.get("restartCount"),
            "resources": usage.get(metadata.get("name")),
        })

    deployment_status = deployment.get("status", {})
    hpa_status = hpa.get("status", {})
    assigned = {pod.get("node") for pod in pod_report if pod.get("node")}
    node_report = []
    for node in (nodes or {}).get("items", []):
        metadata = node.get("metadata", {})
        if metadata.get("name") not in assigned:
            continue
        node_report.append({
            "name": metadata["name"], "created": metadata.get("creationTimestamp"),
            "allocatable": {key: node.get("status", {}).get("allocatable", {}).get(key)
                            for key in ("cpu", "memory", "pods")},
            "pressure": {item.get("type"): item.get("status") for item in node.get("status", {}).get("conditions", [])
                         if item.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure")},
        })
    return {
        "observed_at": observed_at,
        "replicas": {"desired": deployment.get("spec", {}).get("replicas"),
                     "current": deployment_status.get("replicas", 0),
                     "ready": deployment_status.get("readyReplicas", 0),
                     "hpa_desired": hpa_status.get("desiredReplicas"),
                     "hpa_current": hpa_status.get("currentReplicas")},
        "metrics_available": metrics is not None,
        "nodes": node_report,
        "pods": pod_report,
    }


def sample():
    deployment = kubectl("get", "deployment/bedrockgateway", "-o", "json")
    hpa = kubectl("get", "hpa/bedrockgateway", "-o", "json")
    pods = kubectl("get", "pods", "-l", LABEL, "-o", "json")
    events = kubectl("get", "events", "--field-selector", "involvedObject.kind=Pod", "-o", "json")
    endpoints = kubectl("get", "endpointslices", "-l", "kubernetes.io/service-name=bedrockgateway", "-o", "json")
    nodes = kubectl("get", "nodes", "-o", "json")
    metrics = kubectl("get", "--raw", f"/apis/metrics.k8s.io/v1beta1/namespaces/{NAMESPACE}/pods", optional=True)
    if metrics:
        names = {item.get("metadata", {}).get("name") for item in pods.get("items", [])}
        metrics = {"items": [item for item in metrics.get("items", []) if item.get("metadata", {}).get("name") in names]}
    return summarize(deployment, hpa, pods, events, endpoints, metrics,
                     datetime.now(timezone.utc).isoformat(), nodes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="private JSONL evidence path; must not already exist")
    parser.add_argument("--samples", type=int, required=True)
    parser.add_argument("--interval", type=int, required=True, help="seconds between samples")
    args = parser.parse_args()
    if not 1 <= args.samples <= 1440 or not 1 <= args.interval <= 300 or (args.samples - 1) * args.interval > 1440:
        parser.error("samples and interval must be positive and span at most 1440 seconds")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(args.output, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w") as output:
            for index in range(args.samples):
                if index:
                    time.sleep(args.interval)
                output.write(json.dumps(sample(), separators=(",", ":")) + "\n")
                output.flush()
    except Exception:
        raise RuntimeError("Gateway sampling failed; partial private report retained") from None


if __name__ == "__main__":
    main()
