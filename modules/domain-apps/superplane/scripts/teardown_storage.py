"""Remove the explicitly selected installation's retained lifecycle PVC, PV and EBS volume."""

import argparse
import json
import os
import re
import subprocess
import time
from pathlib import Path

from teardown_infrastructure import engine, ensure_quiescent, require, verify_management


def kube(args, *command):
    return subprocess.check_output(
        ["kubectl", "--kubeconfig", args.kubeconfig, *map(str, command)], text=True
    )


def get(args, kind, name, namespace=None):
    command = ["get", kind, name, "--ignore-not-found", "-o", "json"]
    if namespace:
        command += ["-n", namespace]
    raw = kube(args, *command)
    return json.loads(raw) if raw.strip() else None


def validate(pvc, pv, volume, installation, namespace, claim, cluster):
    require(pvc and pv, "Both the original PVC and PV must exist for preparation")
    meta, spec = pvc["metadata"], pv["spec"]
    require(
        meta["namespace"] == namespace
        and meta["name"] == claim
        and meta.get("labels", {}).get("adp.aws-e.io/installation") == installation,
        "PVC installation ownership differs",
    )
    require(
        pvc["spec"]["volumeName"] == pv["metadata"]["name"]
        and spec["claimRef"]["uid"] == meta["uid"]
        and spec["claimRef"]["namespace"] == namespace
        and spec["claimRef"]["name"] == claim
        and spec["persistentVolumeReclaimPolicy"] == "Retain"
        and spec["storageClassName"] == f"superplane-lifecycle-{installation}"
        and spec["csi"]["driver"] == "ebs.csi.eks.amazonaws.com"
        and spec["csi"]["volumeHandle"] == volume["VolumeId"],
        "Retained lifecycle storage binding differs",
    )
    tags = {tag["Key"]: tag["Value"] for tag in volume.get("Tags", [])}
    expected = {
        "eks:eks-cluster-name": cluster,
        "kubernetes.io/created-for/pv/name": pv["metadata"]["name"],
        "kubernetes.io/created-for/pvc/name": claim,
        "kubernetes.io/created-for/pvc/namespace": namespace,
    }
    require(all(tags.get(k) == v for k, v in expected.items()), "EBS ownership differs")
    require(
        volume["State"] == "available" and not volume["Attachments"],
        "EBS volume is attached",
    )


def delete_object(args, directory, original, kind, plural, namespace=None):
    name = original["metadata"]["name"]
    current = get(args, kind, name, namespace)
    if current is None:
        return
    require(
        current["metadata"]["uid"] == original["metadata"]["uid"],
        "Storage object was replaced",
    )
    require(current["spec"] == original["spec"], "Storage binding changed")
    request = directory / f"delete-{kind}.json"
    request.write_text(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {
                    "uid": current["metadata"]["uid"],
                    "resourceVersion": current["metadata"]["resourceVersion"],
                },
            }
        )
    )
    scope = f"namespaces/{namespace}/" if namespace else ""
    kube(args, "delete", "--raw", f"/api/v1/{scope}{plural}/{name}", "-f", request)
    deadline = time.monotonic() + 300
    while get(args, kind, name, namespace) is not None:
        require(
            time.monotonic() < deadline,
            "Storage deletion did not settle; keep receipt and retry",
        )
        time.sleep(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "account",
        "region",
        "environment",
        "installation-id",
        "kubeconfig",
        "claim-namespace",
        "claim-name",
        "output-dir",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--namespace", action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    os.environ.update(AWS_REGION=args.region, AWS_DEFAULT_REGION=args.region)
    require(
        all(
            re.fullmatch(r"[a-z0-9][a-z0-9.-]*", s)
            for s in (args.claim_namespace, args.claim_name)
        ),
        "Invalid claim",
    )
    require(
        args.claim_namespace in args.namespace,
        "Claim namespace is outside selected installation",
    )
    shared = engine()
    require(
        shared.aws("sts", "get-caller-identity")["Account"] == args.account,
        "AWS account differs",
    )
    directory = Path(args.output_dir).resolve()
    if not args.apply:
        require(not directory.exists(), "Use a new private output directory")
        directory.mkdir(parents=True, mode=0o700)
    verify_management(shared, args, directory)
    ensure_quiescent(args.kubeconfig, args.installation_id, args.namespace)
    target = {
        key: getattr(args, key)
        for key in (
            "account",
            "region",
            "environment",
            "installation_id",
            "claim_namespace",
            "claim_name",
        )
    }
    receipt = directory / "storage.json"
    if not args.apply:
        pvc = get(args, "pvc", args.claim_name, args.claim_namespace)
        require(pvc is not None, "Selected PVC does not exist")
        pv = get(args, "pv", pvc["spec"]["volumeName"])
        require(pv is not None, "Selected PV does not exist")
        volume = shared.aws(
            "ec2", "describe-volumes", "--volume-ids", pv["spec"]["csi"]["volumeHandle"]
        )["Volumes"][0]
        platform = json.loads((directory / "management-platform.tfstate").read_text())
        cluster = next(
            a["name"]
            for _, k, a in shared.instances(platform)
            if k == "aws_eks_cluster"
        )
        validate(
            pvc,
            pv,
            volume,
            args.installation_id,
            args.claim_namespace,
            args.claim_name,
            cluster,
        )
        shared.write_json(
            receipt,
            {
                "target": target,
                "pvc": pvc,
                "pv": pv,
                "volume": volume,
                "cluster": cluster,
            },
        )
        print("Prepared retained PVC/PV/EBS deletion; review private storage.json")
        return
    reviewed = json.loads(receipt.read_text())
    require(reviewed["target"] == target, "Reviewed storage target differs")
    original = reviewed["volume"]
    live = shared.aws(
        "ec2",
        "describe-volumes",
        "--volume-ids",
        original["VolumeId"],
        absent=("InvalidVolume.NotFound",),
    )
    if live:
        volume = live["Volumes"][0]
        require(
            volume["CreateTime"] == original["CreateTime"]
            and volume.get("Tags") == original.get("Tags"),
            "Volume identity changed",
        )
        validate(
            reviewed["pvc"],
            reviewed["pv"],
            volume,
            args.installation_id,
            args.claim_namespace,
            args.claim_name,
            reviewed["cluster"],
        )
    delete_object(
        args,
        directory,
        reviewed["pvc"],
        "pvc",
        "persistentvolumeclaims",
        args.claim_namespace,
    )
    delete_object(args, directory, reviewed["pv"], "pv", "persistentvolumes")
    if live:
        current = shared.aws(
            "ec2", "describe-volumes", "--volume-ids", original["VolumeId"]
        )["Volumes"][0]
        require(
            current["State"] == "available" and not current["Attachments"],
            "Volume was reattached",
        )
        shared.aws("ec2", "delete-volume", "--volume-id", original["VolumeId"])
        deadline = time.monotonic() + 120
        while shared.aws(
            "ec2",
            "describe-volumes",
            "--volume-ids",
            original["VolumeId"],
            absent=("InvalidVolume.NotFound",),
        ):
            require(time.monotonic() < deadline, "EBS deletion did not settle")
            time.sleep(3)
    shared.write_json(directory / "complete.json", target)
    print("Retained lifecycle PVC, PV and EBS volume removed and verified")


if __name__ == "__main__":
    main()
