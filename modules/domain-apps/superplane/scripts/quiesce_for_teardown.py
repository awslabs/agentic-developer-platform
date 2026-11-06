"""Pause an explicitly selected installation before infrastructure teardown.

Default is a read-only preview. Apply uses UID/resourceVersion preconditions,
retains completed jobs and all storage, and verifies no workload pods remain.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def command(args):
    return subprocess.check_output(list(map(str, args)), text=True)


def patch_for(item):
    kind, spec = item["kind"], item.get("spec", {})
    changes = []
    if kind in ("ScaledJob", "ScaledObject"):
        annotations = dict(item["metadata"].get("annotations", {}))
        annotations["autoscaling.keda.sh/paused"] = "true"
        if kind == "ScaledObject":
            annotations["autoscaling.keda.sh/paused-replicas"] = "0"
        if annotations != item["metadata"].get("annotations", {}):
            changes.append(
                {"op": "add", "path": "/metadata/annotations", "value": annotations}
            )
    elif kind in ("Deployment", "StatefulSet"):
        if spec.get("replicas", 1) != 0:
            changes.append({"op": "add", "path": "/spec/replicas", "value": 0})
    elif kind in ("Job", "CronJob"):
        terminal = any(
            c.get("status") == "True" and c.get("type") in ("Complete", "Failed")
            for c in item.get("status", {}).get("conditions", [])
        )
        if not terminal and spec.get("suspend") is not True:
            changes.append({"op": "add", "path": "/spec/suspend", "value": True})
    elif kind == "DaemonSet":
        raise ValueError("DaemonSet requires its maintained cleanup before teardown")
    return (
        (
            [
                {
                    "op": "test",
                    "path": "/metadata/uid",
                    "value": item["metadata"]["uid"],
                },
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": item["metadata"]["resourceVersion"],
                },
            ]
            + changes
        )
        if changes
        else []
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "account",
        "region",
        "cluster",
        "installation-id",
        "kubeconfig",
        "output-dir",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--namespace", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    os.umask(0o077)
    os.environ.update(AWS_REGION=args.region, AWS_DEFAULT_REGION=args.region)
    identity = json.loads(
        command(["aws", "sts", "get-caller-identity", "--output", "json"])
    )
    require(
        identity["Account"] == args.account, "AWS account differs from selected target"
    )
    cluster = json.loads(
        command(
            [
                "aws",
                "eks",
                "describe-cluster",
                "--name",
                args.cluster,
                "--region",
                args.region,
                "--output",
                "json",
            ]
        )
    )["cluster"]
    require(
        cluster["arn"]
        == f"arn:aws:eks:{args.region}:{args.account}:cluster/{args.cluster}",
        "EKS target differs",
    )
    kube = ["kubectl", "--kubeconfig", args.kubeconfig]
    config = json.loads(
        command(kube + ["config", "view", "--minify", "--raw", "-o", "json"])
    )
    transport = config["clusters"][0]["cluster"]
    require(
        transport.get("server") == cluster["endpoint"]
        and transport.get("certificate-authority-data")
        == cluster["certificateAuthority"]["data"]
        and not transport.get("insecure-skip-tls-verify"),
        "Kubeconfig differs from selected EKS transport",
    )
    directory = Path(args.output_dir).resolve()
    require(
        not directory.exists(),
        "Use a new private output directory; preserve earlier receipts",
    )
    directory.mkdir(parents=True, mode=0o700)
    selected = []
    namespace_uids = {}
    for namespace in args.namespace:
        observed = json.loads(
            command(kube + ["get", "namespace", namespace, "-o", "json"])
        )
        labels = observed["metadata"].get("labels", {})
        require(
            labels.get("adp.aws-e.io/installation") == args.installation_id
            and labels.get("adp.aws-e.io/domain-app") == "superplane",
            "Namespace is not owned by the selected installation",
        )
        namespace_uids[namespace] = observed["metadata"]["uid"]
        items = json.loads(
            command(
                kube
                + [
                    "get",
                    "deployments,statefulsets,daemonsets,jobs,cronjobs,scaledjobs,scaledobjects",
                    "-n",
                    namespace,
                    "-o",
                    "json",
                ]
            )
        )["items"]
        for item in items:
            patch = patch_for(item)
            if patch:
                require(
                    item["metadata"].get("labels", {}).get("adp.aws-e.io/installation")
                    == args.installation_id,
                    "Active controller is not owned by the selected installation",
                )
                selected.append(
                    {
                        "kind": item["kind"],
                        "namespace": namespace,
                        "name": item["metadata"]["name"],
                        "patch": patch,
                    }
                )
    selected.sort(
        key=lambda x: 0 if x["kind"] in ("ScaledJob", "ScaledObject", "CronJob") else 1
    )
    receipt = {
        "account": args.account,
        "cluster_arn": cluster["arn"],
        "installation_id": args.installation_id,
        "namespaces": namespace_uids,
        "changes": selected,
        "status": "preview",
    }
    path = directory / "quiesce.json"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        f"Selected {len(selected)} controller pauses; storage and completed jobs retained",
        flush=True,
    )
    if not args.apply:
        return 0
    receipt["status"] = "running"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    for item in selected:
        observed = json.loads(
            command(kube + ["get", "namespace", item["namespace"], "-o", "json"])
        )
        require(
            observed["metadata"]["uid"] == namespace_uids[item["namespace"]],
            "Namespace was replaced",
        )
        command(
            kube
            + [
                "patch",
                item["kind"],
                item["name"],
                "-n",
                item["namespace"],
                "--type=json",
                "-p",
                json.dumps(item["patch"]),
            ]
        )
    deadline = time.monotonic() + 300
    while True:
        active = []
        for namespace in args.namespace:
            pods = json.loads(
                command(kube + ["get", "pods", "-n", namespace, "-o", "json"])
            )["items"]
            active.extend(
                p
                for p in pods
                if p.get("status", {}).get("phase") not in ("Succeeded", "Failed")
            )
        if not active:
            break
        require(
            time.monotonic() < deadline,
            "Workloads did not stop; preserve infrastructure and inspect receipt",
        )
        time.sleep(5)
    receipt["status"] = "complete"
    path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        "Selected installation workloads stopped; no resources or storage were deleted",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
