#!/usr/bin/env python3
"""Read selected rollout and ARC status fields in the reviewed dev cluster.

No logs, environment, Secret/ConfigMap reads, exec, SQL, Terraform or runtime
mutations. kubectl formats an explicit field projection; raw API objects and
provider errors are never emitted or retained. Kubeconfig is task-local.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import tempfile

ACCOUNT = "879318057152"
REGION = "us-east-1"
CLUSTER = "adp-dev-eks-cluster"
ARN = f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{CLUSTER}"
NAMESPACES = ("adp-gateway", "arc-runners", "arc-systems")


class DiagnosticError(Exception):
    pass


def run(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=40, check=False)
    except subprocess.TimeoutExpired:
        raise DiagnosticError("read_timeout") from None
    except OSError:
        raise DiagnosticError("tool_unavailable") from None
    if result.returncode:
        category = next((code for code in ("Forbidden", "Unauthorized", "NotFound", "AccessDenied", "ValidationError") if code in result.stderr), "command_failed")
        raise DiagnosticError(category)
    return result.stdout


def scalar(path):
    # Print only a named scalar. Quoting protects the JSON framing; all textual
    # fields selected below are Kubernetes identities, timestamps or reason codes.
    return '{{printf "%q" (printf "%v" ' + path + ')}}'


def record(fields, arrays=None):
    parts = [json.dumps(key) + ":" + scalar(path) for key, path in fields.items()]
    for key, (path, child) in (arrays or {}).items():
        parts.append(json.dumps(key) + ":[{{range $i, $v := " + path + "}}{{if $i}},{{end}}" + child + "{{end}}]")
    return "{" + ",".join(parts) + "}"


CONDITIONS = record({"type": ".type", "status": ".status", "reason": ".reason", "last_transition_at": ".lastTransitionTime"})
CONTAINERS = record({
    "name": ".name", "ready": ".ready", "restart_count": ".restartCount",
    "waiting_reason": ".state.waiting.reason", "started_at": ".state.running.startedAt",
    "terminated_reason": ".state.terminated.reason", "exit_code": ".state.terminated.exitCode",
    "finished_at": ".state.terminated.finishedAt", "previous_reason": ".lastState.terminated.reason",
    "previous_exit_code": ".lastState.terminated.exitCode",
})
IDENTITY = {"name": ".metadata.name", "namespace": ".metadata.namespace", "created_at": ".metadata.creationTimestamp"}
PROJECTIONS = {
    "pods": record(IDENTITY | {
        "node": ".spec.nodeName", "phase": ".status.phase", "reason": ".status.reason", "deleting_at": ".metadata.deletionTimestamp",
    }, {"conditions": (".status.conditions", CONDITIONS), "containers": (".status.containerStatuses", CONTAINERS),
        "init_containers": (".status.initContainerStatuses", CONTAINERS)}),
    "jobs": record(IDENTITY | {
        "active": ".status.active", "succeeded": ".status.succeeded", "failed": ".status.failed",
        "started_at": ".status.startTime", "completed_at": ".status.completionTime",
    }, {"conditions": (".status.conditions", CONDITIONS)}),
    "nodes": record({
        "name": ".metadata.name", "unschedulable": ".spec.unschedulable",
        "allocatable_cpu": ".status.allocatable.cpu", "allocatable_memory": ".status.allocatable.memory",
        "allocatable_pods": ".status.allocatable.pods",
    }, {"conditions": (".status.conditions", CONDITIONS),
        "taints": (".spec.taints", record({"key": ".key", "effect": ".effect"}))}),
    "events": record({
        "kind": ".involvedObject.kind", "name": ".involvedObject.name", "namespace": ".involvedObject.namespace",
        "type": ".type", "reason": ".reason", "count": ".count", "first_at": ".firstTimestamp",
        "last_at": ".lastTimestamp", "event_at": ".eventTime", "series_last_at": ".series.lastObservedTime",
    }),
}
COLLECTIONS = tuple((kind, ns) for ns in NAMESPACES for kind in ("pods", "events")) + (("jobs", "adp-gateway"), ("nodes", None))


def decode(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        raise DiagnosticError("invalid_projected_response") from None


def normalize(value):
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items()}
    return None if value in ("<nil>", "<no value>") else value


def collect(kind, namespace):
    if (kind, namespace) not in COLLECTIONS:
        raise DiagnosticError("unapproved_collection")
    template = "[{{range $i, $v := .items}}{{if $i}},{{end}}" + PROJECTIONS[kind] + "{{end}}]"
    args = ["kubectl", "--request-timeout=30s", "get", kind, "--chunk-size=200", "-o", "go-template=" + template]
    if namespace:
        args += ["--namespace", namespace]
    items = normalize(decode(run(args)))
    if not isinstance(items, list):
        raise DiagnosticError("invalid_projected_response")
    if kind == "jobs":
        items = [item for item in items if str(item.get("name", "")).startswith("gateway-migrate-")]
    if kind == "events":
        items = [item for item in items if item.get("kind") in {"Pod", "Job", "Node", "Deployment", "ReplicaSet", "EphemeralRunner", "EphemeralRunnerSet", "AutoscalingRunnerSet"}]
    items.sort(key=lambda item: item.get("series_last_at") or item.get("last_at") or item.get("event_at") or item.get("created_at") or item.get("name") or "")
    return {"status": "observed", "total": len(items), "truncated": len(items) > 200, "items": items[-200:]}


def identity(account_id, scratch):
    if account_id != ACCOUNT:
        raise DiagnosticError("incorrect_target_account")
    who = decode(run(["aws", "sts", "get-caller-identity", "--region", REGION, "--output", "json"]))
    if who.get("Account") != ACCOUNT:
        raise DiagnosticError("incorrect_caller_account")
    cluster = decode(run(["aws", "eks", "describe-cluster", "--name", CLUSTER, "--region", REGION,
                          "--query", "cluster.{arn:arn,endpoint:endpoint,status:status}", "--output", "json"]))
    if cluster.get("arn") != ARN or cluster.get("status") != "ACTIVE" or not str(cluster.get("endpoint", "")).startswith("https://"):
        raise DiagnosticError("cluster_identity_mismatch")
    os.environ["KUBECONFIG"] = str(scratch / "kubeconfig")
    run(["aws", "eks", "update-kubeconfig", "--name", CLUSTER, "--region", REGION, "--kubeconfig", os.environ["KUBECONFIG"], "--alias", ARN])
    projection = '{"context":{{printf "%q" (index . "current-context")}},"cluster":{{range .contexts}}{{printf "%q" .context.cluster}}{{end}},"server":{{range .clusters}}{{printf "%q" .cluster.server}}{{end}}}'
    current = decode(run(["kubectl", "config", "view", "--minify", "-o", "go-template=" + projection]))
    if current != {"context": ARN, "cluster": ARN, "server": cluster["endpoint"]}:
        raise DiagnosticError("kube_identity_mismatch")
    return {"account_id": ACCOUNT, "cluster_arn": ARN, "stage": "diagnose"}


def diagnose(account_id, directory):
    evidence = {"observed_at": datetime.now(UTC).isoformat(), "read_only": True, "collections": {}}
    with tempfile.TemporaryDirectory(prefix="adp-runtime-diagnose-") as scratch:
        evidence["identity"] = identity(account_id, Path(scratch))
        for kind, namespace in COLLECTIONS:
            try:
                observation = collect(kind, namespace)
            except DiagnosticError as error:
                observation = {"status": "unavailable", "reason": str(error)}
            evidence["collections"][f"{namespace or 'cluster'}/{kind}"] = observation
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "diagnostics.json"
    destination.write_text(json.dumps(evidence, indent=2) + "\n")
    destination.chmod(0o600)
    print(json.dumps(evidence))
    return all(item["status"] == "observed" for item in evidence["collections"].values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--evidence-directory", required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    os.environ["AWS_PAGER"] = ""
    try:
        raise SystemExit(0 if diagnose(args.account_id, args.evidence_directory) else 1)
    except DiagnosticError as error:
        raise SystemExit(str(error)) from None
