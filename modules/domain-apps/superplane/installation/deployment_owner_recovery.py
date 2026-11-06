"""Explicit reviewed recovery of a stopped installation's stale image fences.

The immutable receipt remains unchanged. Render the exact selected clean source,
verify retained lock and completed Jobs, and use only ordinary server-side apply.
"""

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import boto3
import yaml

from .config import LABEL, digest, require
from .runner import local_lock


RENDER = """import json,sys
from installation.config import digest
from installation.manifests import render
r=json.load(sys.stdin)
e=dict(r['environment'])
e['management_api_server']=r['management_api_server']
e['cluster_dns_ip']=r['management_dns_configuration']['resolver']
docs=render(e,r['release_lock'],control_plane_only=r.get('mode')=='control-plane-only')
for d in docs:
 if d['kind']=='Deployment':
  d['spec']['template']['metadata']['annotations']['adp.aws-e.io/secret-versions']=digest(r['secret_versions'])
json.dump([d for d in docs if d['kind']=='Deployment'],sys.stdout)
"""


def prepare(receipt, desired, current):
    name, namespace = desired["metadata"]["name"], desired["metadata"]["namespace"]
    entries = [
        e
        for e in receipt["environment"].get("deployment_image_recovery", [])
        if e["name"] == name and e["namespace"] == namespace
    ]
    require(len(entries) == 1, "Only an originally declared recovery target is allowed")
    meta = current["metadata"]
    require(
        meta["name"] == name
        and meta["namespace"] == namespace
        and meta["uid"] == entries[0]["uid"]
        and meta.get("labels", {}).get(LABEL) == receipt["installation_id"]
        and not meta.get("deletionTimestamp"),
        "Deployment identity/owner differs",
    )
    old = {
        c["name"]: c["image"] for c in current["spec"]["template"]["spec"]["containers"]
    }
    new = {
        c["name"]: c["image"] for c in desired["spec"]["template"]["spec"]["containers"]
    }
    require(set(old) <= set(new), "Recovery cannot remove a container")
    changed = {k for k in old if old[k] != new[k]}
    require(changed, "No existing image change needs recovery")
    for name in changed:
        owners = []
        for fields in meta.get("managedFields", []):
            containers = (
                fields.get("fieldsV1", {})
                .get("f:spec", {})
                .get("f:template", {})
                .get("f:spec", {})
                .get("f:containers", {})
            )
            if "f:image" in containers.get('k:{"name":"' + name + '"}', {}):
                owners.append(
                    (
                        fields.get("manager"),
                        fields.get("operation"),
                        fields.get("subresource", ""),
                    )
                )
        require(
            owners == [("superplane-installer", "Apply", "")],
            "Image is not exclusively owned by the maintained installer",
        )
    apply = copy.deepcopy(desired)
    apply["metadata"].update(uid=meta["uid"], resourceVersion=meta["resourceVersion"])
    review = {
        "name": meta["name"],
        "namespace": namespace,
        "uid": meta["uid"],
        "resource_version": meta["resourceVersion"],
        "prior_spec_sha256": digest(current["spec"]),
        "desired_sha256": digest(desired),
        "prior_images": old,
        "desired_images": new,
        "changed_existing_images": sorted(changed),
        "added_containers": sorted(set(new) - set(old)),
    }
    return review, apply


def check_receipt(receipt, stopped):
    require(
        stopped == receipt["run_id"]
        and receipt["status"] == "recovery-required"
        and receipt.get("stage") == "rollout"
        and {"migration", "bootstrap"} <= set(receipt.get("completed", []))
        and receipt.get("bootstrap", {}).get("token_secret_removed") is True
        and receipt.get("public_route_enabled") is False
        and receipt.get("remote_lock"),
        "Stopped post-bootstrap locked rollout required",
    )
    require(
        receipt["release_id"] == digest(receipt["release_lock"]),
        "Release identity differs",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="Clean checkout of the receipt selected installation revision",
    )
    parser.add_argument("--confirm-stopped", required=True)
    parser.add_argument("--approved-review-sha256")
    args = parser.parse_args()
    with local_lock(args.receipt.parent):
        raw = args.receipt.read_bytes()
        receipt = json.loads(raw)
        check_receipt(receipt, args.confirm_stopped)
        source = args.source.resolve()
        sha = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        require(
            sha == receipt["release_lock"]["source_revision"]
            and not subprocess.check_output(
                ["git", "-C", str(source), "status", "--porcelain"], text=True
            ).strip(),
            "Selected source differs or is dirty",
        )
        docs = json.loads(
            subprocess.check_output(
                [sys.executable, "-c", RENDER],
                input=json.dumps(receipt),
                text=True,
                cwd=source / "modules/domain-apps/superplane",
                env=dict(os.environ, PYTHONPATH=".:contracts"),
            )
        )
        env = receipt["environment"]
        session = boto3.Session(region_name=env["region"])
        identity = env["deployment_identity"]
        sts, iam, eks, s3 = [
            session.client(service) for service in ["sts", "iam", "eks", "s3"]
        ]
        kube = [
            "kubectl",
            "--request-timeout=30s",
            "--kubeconfig",
            str(args.receipt.parent / "kubeconfig"),
        ]

        def read(*parts):
            return json.loads(subprocess.check_output([*kube, *parts], text=True))

        def authority():
            caller = sts.get_caller_identity()
            role = iam.get_role(
                RoleName=identity["expected_role_arn"].rsplit("/", 1)[1]
            )["Role"]
            require(
                caller["Account"] == env["account_id"]
                and caller["UserId"].split(":")[0] == identity["expected_role_id"]
                and role["RoleId"] == identity["expected_role_id"]
                and role["Arn"] == identity["expected_role_arn"],
                "Installer identity differs",
            )
            lock = receipt["remote_lock"]
            require(
                lock["bucket"] == "adp-terraform-state-" + env["account_id"]
                and lock["key"]
                == env["environment"] + "/modules/superplane/installation.lock",
                "Lock target differs",
            )
            current = s3.get_object(Bucket=lock["bucket"], Key=lock["key"])
            value = json.loads(current["Body"].read())
            expected = json.loads(
                (args.receipt.parent / "installation-lock.json").read_text()
            )
            require(
                current["ETag"] == lock["etag"]
                and value == expected
                and value["run_id"] == receipt["run_id"]
                and value["installation_id"] == receipt["installation_id"],
                "Retained lock differs",
            )
            require(args.receipt.read_bytes() == raw, "Receipt changed during recovery")

        authority()
        cluster = eks.describe_cluster(name=env["cluster"])["cluster"]
        config = read("config", "view", "--raw", "--minify", "-o", "json")["clusters"][
            0
        ]["cluster"]
        require(
            config["server"] == cluster["endpoint"]
            and config.get("certificate-authority-data")
            == cluster["certificateAuthority"]["data"]
            and not config.get("insecure-skip-tls-verify"),
            "Kubernetes target differs",
        )
        for phase in ["migrate", "bootstrap"]:
            name = f"superplane-{phase}-{receipt['run_id']}"
            recorded = [
                x
                for x in receipt["objects"]
                if x["kind"] == "Job"
                and x["name"] == name
                and x["namespace"] == env["namespace"]
            ]
            job = read("-n", env["namespace"], "get", "job", name, "-o", "json")
            require(
                len(recorded) == 1
                and recorded[0]["uid"] == job["metadata"]["uid"]
                and job["metadata"]["labels"].get(LABEL) == receipt["installation_id"]
                and any(
                    c["type"] == "Complete" and c["status"] == "True"
                    for c in job["status"].get("conditions", [])
                ),
                "Original Job completion differs",
            )
        selected = []
        for entry in env.get("deployment_image_recovery", []):
            desired = next(
                d
                for d in docs
                if d["metadata"]["name"] == entry["name"]
                and d["metadata"]["namespace"] == entry["namespace"]
            )
            current = read(
                "-n",
                entry["namespace"],
                "get",
                "deployment",
                entry["name"],
                "-o",
                "json",
                "--show-managed-fields",
            )
            old = {
                c["name"]: c["image"]
                for c in current["spec"]["template"]["spec"]["containers"]
            }
            new = {
                c["name"]: c["image"]
                for c in desired["spec"]["template"]["spec"]["containers"]
            }
            if old == new:
                continue
            review, apply = prepare(receipt, desired, current)
            # Every field, including the selected new images, must pass ordinary
            # server-side apply. This command never forces a field conflict.
            subprocess.run(
                [
                    *kube,
                    "apply",
                    "--server-side",
                    "--dry-run=server",
                    "--field-manager=superplane-installer",
                    "-f",
                    "-",
                    "-o",
                    "json",
                ],
                input=yaml.safe_dump(apply),
                text=True,
                stdout=subprocess.DEVNULL,
                check=True,
                timeout=60,
            )
            selected.append((review, apply))
        require(selected, "No image recovery remains")
        review = {
            "run_id": receipt["run_id"],
            "release_id": receipt["release_id"],
            "source_revision": sha,
            "approved_installation_plan": receipt["plan_sha256"],
            "targets": [item[0] for item in selected],
        }
        pin = digest(review)
        print(json.dumps({"review": review, "sha256": pin}), flush=True)
        if args.approved_review_sha256 is None:
            return
        require(args.approved_review_sha256 == pin, "Current deployment review differs")
        for target, apply in selected:
            authority()
            current = read(
                "-n",
                target["namespace"],
                "get",
                "deployment",
                target["name"],
                "-o",
                "json",
            )
            require(
                current["metadata"]["uid"] == target["uid"]
                and current["metadata"]["resourceVersion"] == target["resource_version"]
                and digest(current["spec"]) == target["prior_spec_sha256"],
                "Deployment changed before apply",
            )
            subprocess.run(
                [
                    *kube,
                    "apply",
                    "--server-side",
                    "--field-manager=superplane-installer",
                    "-f",
                    "-",
                    "-o",
                    "json",
                ],
                input=yaml.safe_dump(apply),
                text=True,
                stdout=subprocess.DEVNULL,
                check=True,
                timeout=60,
            )
            live = read(
                "-n",
                target["namespace"],
                "get",
                "deployment",
                target["name"],
                "-o",
                "json",
            )
            require(
                live["metadata"]["uid"] == target["uid"]
                and {
                    c["name"]: c["image"]
                    for c in live["spec"]["template"]["spec"]["containers"]
                }
                == target["desired_images"],
                "Applied image readback differs",
            )
            print(
                json.dumps(
                    {"name": target["name"], "status": "applied-images-verified"}
                ),
                flush=True,
            )
        print(
            "Resume the original installer for rollout and complete verification.",
            flush=True,
        )


if __name__ == "__main__":
    main()
