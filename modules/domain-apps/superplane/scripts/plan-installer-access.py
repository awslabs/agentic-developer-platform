#!/usr/bin/env python3
"""Prepare a private saved Terraform plan for app-owned installer access.

This entrypoint never applies a plan or changes existing shared access entries.
The output is private; it may contain target identifiers and IAM policy details.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    values = json.loads(args.configuration.read_text())
    expected = {"account_id", "region", "environment", "cluster_name", "installation_id", "operator_role_arn", "operator_role_id", "namespaces", "installer_policy_json", "namespace_bootstrap"}
    if set(values) != expected:
        raise SystemExit("Configuration must contain exactly the module's explicit inputs")
    if not re.fullmatch(r"[0-9]{12}", values["account_id"]) or not re.fullmatch(r"[a-f0-9]{24}", values["installation_id"]):
        raise SystemExit("Invalid installation target identity")
    for key in ("environment", "region", "cluster_name"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", values[key]):
            raise SystemExit("Invalid target configuration")
    prefix = ["aws", "--region", values["region"], "--no-cli-pager"]

    def read_aws(*command):
        return json.loads(subprocess.check_output([*prefix, *command, "--output", "json"]))

    caller = read_aws("sts", "get-caller-identity")
    role = read_aws("iam", "get-role", "--role-name", values["operator_role_arn"].rsplit("/", 1)[1])["Role"]
    if caller["Account"] != values["account_id"] or role["Arn"] != values["operator_role_arn"] or role["RoleId"] != values["operator_role_id"] or not caller["UserId"].startswith(role["RoleId"] + ":"):
        raise SystemExit("Active command adapter is not the selected immutable operator identity")
    target_role = f"adp-{values['environment']}-superplane-installer-{values['installation_id']}"
    # Initial ownership refuses an existing role. The caller must reconcile a
    # retained app-owned state before proposing an upgrade, never import by name.
    check = subprocess.run([*prefix, "iam", "get-role", "--role-name", target_role, "--output", "json"], text=True, capture_output=True)
    if check.returncode == 0 or "NoSuchEntity" not in check.stderr:
        raise SystemExit("Target role exists or absence cannot be established; reconcile its state owner first")
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(output, 0o700)
    source = Path(__file__).resolve().parents[1] / "infra" / "installer-access"
    hashes = {}
    for original in source.glob("*.tf"):
        raw = original.read_bytes()
        (output / original.name).write_bytes(raw)
        hashes[original.name] = hashlib.sha256(raw).hexdigest()
    lock = source / ".terraform.lock.hcl"
    if lock.exists():
        shutil.copyfile(lock, output / lock.name)
        hashes[lock.name] = hashlib.sha256(lock.read_bytes()).hexdigest()
    (output / "installation.auto.tfvars.json").write_text(json.dumps(values, indent=2) + "\n")
    backend = {"bucket": "adp-terraform-state-" + values["account_id"], "key": f"{values['environment']}/modules/superplane-installer-access/{values['installation_id']}/terraform.tfstate", "region": values["region"], "encrypt": True, "dynamodb_table": "adp-terraform-locks"}
    (output / "backend.json").write_text(json.dumps(backend, indent=2) + "\n")
    for path in output.iterdir():
        path.chmod(0o600)
    tf = ["terraform", f"-chdir={output}"]
    with (output / "terraform.log").open("w") as log:
        subprocess.run([*tf, "init", "-input=false", "-backend-config=backend.json"], stdout=log, stderr=subprocess.STDOUT, check=True)
        # Planning is read-only: no remote state lock object is written. Apply
        # must use normal locking and reject changed source/state/plan bytes.
        subprocess.run([*tf, "plan", "-input=false", "-lock=false", "-out=installation.tfplan"], stdout=log, stderr=subprocess.STDOUT, check=True)
    raw = subprocess.check_output([*tf, "show", "-json", "installation.tfplan"])
    (output / "plan.json").write_bytes(raw)
    plan = json.loads(raw)
    changes = [{"address": r["address"], "actions": r["change"]["actions"]} for r in plan.get("resource_changes", []) if r.get("mode") == "managed"]
    allowed = {"aws_iam_role.installer", "aws_iam_role_policy.installer", "aws_eks_access_entry.installer", "aws_eks_access_policy_association.installer", "aws_eks_access_policy_association.read"}
    if {r["address"] for r in changes} != allowed or any(r["actions"] != ["create"] for r in changes):
        raise SystemExit("Initial access plan must create only the five app-owned installer resources")
    report = {"status": "planned-not-approved", "source_files": hashes, "operator": {"account": caller["Account"], "role_arn": role["Arn"], "role_id": role["RoleId"]}, "backend": backend, "changes": changes, "namespace_bootstrap": values["namespace_bootstrap"], "plan_sha256": hashlib.sha256((output / "installation.tfplan").read_bytes()).hexdigest(), "shared_machine_access_modified": False, "applied": False}
    (output / "review.json").write_text(json.dumps(report, indent=2) + "\n")
    for path in output.iterdir():
        if path.is_file():
            path.chmod(0o600)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
