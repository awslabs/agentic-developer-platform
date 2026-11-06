"""Prepare and apply exact destroy plans for installation-owned Superplane roots.

This is full installation decommissioning, not workspace retirement. Workspace
infrastructure uses infra/workspaces/scripts/{prepare,apply}_workspace_plan.py.
Retained foundations require a separate explicit --include-retained choice.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parents[2]
KINDS = {
    "dedicated-readers": ("dedicated-readers", 2),
    "domain-runtime": ("domain-runtime", 3),
    "lifecycle-foundations": ("lifecycle-foundations", 2),
    "domain-provider": ("domain-provider", 0),
    "installer-namespace": ("installer-access/namespace-rbac", 1),
}
IDENTITY_FIELDS = (
    "id",
    "arn",
    "unique_id",
    "policy_id",
    "created_at",
    "creation_date",
    "tags",
    "tags_all",
)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def engine():
    spec = importlib.util.spec_from_file_location(
        "adp_teardown_shared", ROOT / "platform/scripts/teardown.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_key(kind, values, environment):
    prefix = f"{environment}/modules/superplane-"
    if kind in ("domain-provider", "lifecycle-foundations"):
        for field in ("org_id", "workspace_id"):
            require(
                re.fullmatch(
                    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",
                    values.get(field, ""),
                ),
                "An immutable organization/workspace UUID is required",
            )
        return f"{prefix}{kind}/v1/{values['org_id']}/{values['workspace_id']}/terraform.tfstate"
    if kind == "installer-namespace":
        require(
            re.fullmatch(r"[0-9a-f]{24}", values.get("installation_id", "")),
            "Invalid installation ID",
        )
        return f"{prefix}installer-access/{values['installation_id']}/namespace-rbac/terraform.tfstate"
    return f"{prefix}{kind}/terraform.tfstate"


def check_variables(kind, values, account, region, environment):
    if kind != "installer-namespace":
        require(
            values.get("account_id") == account
            and values.get("environment") == environment,
            "Original inputs differ from selected account/environment",
        )
        require(
            values.get("aws_region", values.get("region")) == region,
            "Original inputs differ from selected region",
        )
    return expected_key(kind, values, environment)


def source_digest(module):
    digest = hashlib.sha256()
    for path in sorted(module.glob("*")):
        if path.is_file() and (
            path.suffix == ".tf"
            or path.name.endswith(".tf.json")
            or path.name == ".terraform.lock.hcl"
        ):
            digest.update(path.name.encode() + b"\0" + path.read_bytes())
    return digest.hexdigest()


def declarations(module):
    return {
        kind + "." + name
        for path in module.glob("*.tf")
        for kind, name in re.findall(
            r'resource\s+"([a-z0-9_]+)"\s+"([a-z0-9_]+)"\s*\{', path.read_text()
        )
    }


def validate_plan(plan, state, allowed, account, shared):
    original = {a: (kind, attrs) for a, kind, attrs in shared.instances(state)}
    rows = shared.deletions(plan)
    for row in rows:
        address = row["address"]
        base = address.split("[", 1)[0]
        require(
            base in allowed and address in original,
            "Destroy contains an unfamiliar resource: " + address,
        )
        kind, before = original[address]
        actual = row["change"]["before"] or {}
        require(row["type"] == kind, "Resource type changed: " + address)
        for field in IDENTITY_FIELDS:
            if before.get(field) is not None:
                require(
                    actual.get(field) == before[field],
                    "Resource identity changed: " + address + "." + field,
                )
        arn = actual.get("arn")
        if kind.startswith("aws_") and arn:
            require(
                arn.split(":")[4] == account,
                "Destroy contains another account's resource",
            )
        if kind.startswith("kubernetes_"):
            require(
                kind == "kubernetes_storage_class_v1",
                "Remove temporary installer grants through their recorded cleanup path first",
            )
            old = before.get("metadata", [{}])[0]
            new = actual.get("metadata", [{}])[0]
            require(
                old.get("uid") and old["uid"] == new.get("uid"),
                "StorageClass identity changed",
            )
            require(
                old.get("labels") == new.get("labels"),
                "StorageClass ownership labels changed",
            )
    return rows


def validate_foundation_owner(values, state, shared):
    expected = {
        "ManagedBy": "superplane-lifecycle-foundations",
        "Environment": values["environment"],
        "SuperplaneFoundationOrgId": values["org_id"],
        "SuperplaneFoundationWorkspaceId": values["workspace_id"],
        "Lifecycle": "retained-independent-owner",
    }
    rows = list(shared.instances(state))
    allowed = {
        "aws_iam_role.actor",
        "aws_kms_key.retained",
        "aws_kms_alias.retained",
        "terraform_data.verified_owner",
    }
    for address, kind, attrs in rows:
        require(
            address.split("[", 1)[0] in allowed,
            "Foundation state contains an unfamiliar address",
        )
        if kind in ("aws_iam_role", "aws_kms_key"):
            require(
                all(attrs.get("tags", {}).get(k) == v for k, v in expected.items()),
                "Foundation ownership tags differ",
            )
        if kind == "aws_iam_role":
            actor = attrs["tags"].get("BootstrapActor")
            require(
                actor in values["actor_role_names"]
                and attrs["name"] == values["actor_role_names"][actor],
                "Foundation actor differs from the original owner inputs",
            )
        if kind == "terraform_data":
            require(
                attrs.get("input", {}).get("value")
                == {
                    "account_id": values["account_id"],
                    "org_id": values["org_id"],
                    "workspace_id": values["workspace_id"],
                    "provider_role": values["provider_role_arn"],
                    "provider_role_id": values["expected_provider_role_id"],
                },
                "Recorded foundation owner differs from original inputs",
            )
        if kind == "aws_kms_alias":
            keys = {a["id"] for _, k, a in rows if k == "aws_kms_key"}
            require(
                attrs["target_key_id"] in keys,
                "Foundation alias refers to an independently owned key",
            )


def state_at(shared, bucket, key, account, path):
    result = shared.aws(
        "s3api",
        "get-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--expected-bucket-owner",
        account,
        str(path),
        absent=("NoSuchKey",),
    )
    if result is None:
        return {"resources": []}
    state = json.loads(path.read_text())
    require(isinstance(state.get("resources"), list), "Malformed state: " + key)
    return state


def check_dependencies(shared, kind, values, bucket, account, environment, directory):
    if kind not in ("domain-provider", "lifecycle-foundations"):
        return
    suffix = f"{values['org_id']}/{values['workspace_id']}/terraform.tfstate"
    keys = [f"{environment}/modules/superplane-workspaces/v2/{suffix}"]
    if kind == "domain-provider":
        keys.append(
            f"{environment}/modules/superplane-lifecycle-foundations/v1/{suffix}"
        )
    for index, key in enumerate(keys):
        state = state_at(
            shared, bucket, key, account, directory / f"dependency-{index}.tfstate"
        )
        require(
            not list(shared.instances(state)),
            "A workspace/foundation dependency still exists: " + key,
        )


def ensure_quiescent(kubeconfig, installation, namespaces):
    require(
        kubeconfig and namespaces and installation,
        "Supply the selected installation and management kubeconfig/namespaces",
    )
    for namespace in namespaces:
        kube = ["kubectl", "--kubeconfig", kubeconfig]
        obj = json.loads(
            subprocess.check_output(
                kube + ["get", "namespace", namespace, "-o", "json"], text=True
            )
        )
        require(
            obj["metadata"].get("labels", {}).get("adp.aws-e.io/installation")
            == installation,
            "Namespace installation identity differs",
        )
        pods = json.loads(
            subprocess.check_output(
                kube + ["get", "pods", "-n", namespace, "-o", "json"], text=True
            )
        )["items"]
        require(
            all(
                p.get("status", {}).get("phase") in ("Succeeded", "Failed")
                for p in pods
            ),
            "Quiesce the installation before infrastructure teardown",
        )


def verify_management(shared, args, directory, check_namespaces=True):
    bucket = "adp-terraform-state-" + args.account
    platform = state_at(
        shared,
        bucket,
        f"{args.environment}/platform/terraform.tfstate",
        args.account,
        directory / "management-platform.tfstate",
    )
    app = state_at(
        shared,
        bucket,
        f"{args.environment}/modules/superplane/terraform.tfstate",
        args.account,
        directory / "management-superplane.tfstate",
    )
    clusters = [
        a for _, kind, a in shared.instances(platform) if kind == "aws_eks_cluster"
    ]
    namespaces = {
        a["value"]
        for _, kind, a in shared.instances(app)
        if kind == "aws_ssm_parameter"
        and a.get("name", "").endswith(
            ("/superplane/namespace", "/superplane/skypilot-namespace")
        )
    }
    require(
        len(clusters) == 1
        and (not check_namespaces or namespaces and set(args.namespace) == namespaces),
        "Select the complete management namespace set recorded in core state",
    )
    cluster = shared.aws("eks", "describe-cluster", "--name", clusters[0]["name"])[
        "cluster"
    ]
    require(
        cluster["arn"] == clusters[0]["arn"]
        and cluster["arn"].split(":")[4] == args.account,
        "Management cluster identity differs from core state",
    )
    config = json.loads(
        subprocess.check_output(
            [
                "kubectl",
                "--kubeconfig",
                args.kubeconfig,
                "config",
                "view",
                "--minify",
                "--raw",
                "-o",
                "json",
            ],
            text=True,
        )
    )
    transport = config["clusters"][0]["cluster"]
    require(
        transport.get("server") == cluster["endpoint"]
        and transport.get("certificate-authority-data")
        == cluster["certificateAuthority"]["data"]
        and not transport.get("insecure-skip-tls-verify"),
        "Kubeconfig does not address the state-owned management cluster",
    )


def run_tf(binary, module, directory, label, *args):
    result = subprocess.run(
        [binary, *map(str, args)],
        cwd=module,
        text=True,
        capture_output=True,
        check=False,
    )
    (directory / (label + ".log")).write_text(result.stdout + result.stderr)
    require(
        result.returncode == 0, "Terraform failed; inspect private " + label + ".log"
    )
    return result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "account",
        "region",
        "environment",
        "state-key",
        "variables",
        "output-dir",
    ):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--kind", choices=KINDS, required=True)
    parser.add_argument("--terraform-binary", default="terraform")
    parser.add_argument("--include-retained", action="store_true")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the saved proposal in output-dir without replanning",
    )
    parser.add_argument("--kubeconfig")
    parser.add_argument("--installation-id")
    parser.add_argument("--namespace", action="append", default=[])
    args = parser.parse_args(argv)
    os.umask(0o077)
    require(
        not any(k.startswith("TF_CLI_ARGS") for k in os.environ),
        "Unset implicit Terraform CLI arguments",
    )
    os.environ.update(AWS_REGION=args.region, AWS_DEFAULT_REGION=args.region)
    shared = engine()
    require(
        shared.aws("sts", "get-caller-identity")["Account"] == args.account,
        "Active AWS account differs",
    )
    values = json.loads(Path(args.variables).read_text())
    key = check_variables(
        args.kind, values, args.account, args.region, args.environment
    )
    require(
        key == args.state_key, "Original inputs do not select the requested state key"
    )
    require(
        args.kind != "lifecycle-foundations" or args.include_retained,
        "Full decommissioning of retained foundations requires --include-retained",
    )
    directory = Path(args.output_dir).resolve()
    bucket = "adp-terraform-state-" + args.account
    if not args.apply:
        require(
            not directory.exists(), "Use a new output directory; preserve earlier plans"
        )
        directory.mkdir(parents=True, mode=0o700)
    require(directory.is_dir(), "Prepare a saved proposal first")
    if args.kind == "installer-namespace":
        if not args.kubeconfig:
            args.kubeconfig = values.get("kubeconfig_path")
        require(
            args.kubeconfig and args.kubeconfig == values.get("kubeconfig_path"),
            "StorageClass inputs and selected management kubeconfig differ",
        )
        verify_management(shared, args, directory, check_namespaces=args.apply)
    if args.apply:
        check_dependencies(
            shared, args.kind, values, bucket, args.account, args.environment, directory
        )
    module = directory / "module"
    plan_path = directory / "destroy.tfplan"
    manifest_path = directory / "review.json"
    target = {
        "account": args.account,
        "region": args.region,
        "environment": args.environment,
        "state_key": key,
        "kind": args.kind,
        "include_retained": args.include_retained,
    }
    if not args.apply:
        source, protection_count = KINDS[args.kind]
        source = APP / "infra" / source
        if args.kind == "lifecycle-foundations":
            shutil.copytree(source / "decommission", module)
            for name in ("variables.tf", ".terraform.lock.hcl"):
                if (source / name).exists():
                    shutil.copyfile(source / name, module / name)
        else:
            shutil.copytree(
                source,
                module,
                ignore=shutil.ignore_patterns(
                    ".terraform",
                    "*.tfstate*",
                    "*.tfplan",
                    "*.tfvars*",
                    "*override.tf*",
                    "__pycache__",
                ),
            )
            main = module / "main.tf"
            original = main.read_text()
            modified, count = re.subn(
                r"prevent_destroy\s*=\s*true", "prevent_destroy = false", original
            )
            require(
                count == protection_count,
                "Protection definitions changed; review app decommissioning support",
            )
            # Preserve creation preconditions for roots whose dependencies remain.
            main.write_text(modified)
        shared.write_json(
            directory / "backend.json",
            {
                "bucket": bucket,
                "key": key,
                "region": args.region,
                "encrypt": True,
                "dynamodb_table": "adp-terraform-locks",
            },
        )
        run_tf(
            args.terraform_binary,
            module,
            directory,
            "init",
            "init",
            "-input=false",
            "-reconfigure",
            "-backend-config=" + str(directory / "backend.json"),
        )
        state = json.loads(
            run_tf(
                args.terraform_binary,
                module,
                directory,
                "state-before",
                "state",
                "pull",
            )
        )
        require(
            list(shared.instances(state)),
            "Selected state is empty; there is nothing to delete",
        )
        if args.kind == "lifecycle-foundations":
            validate_foundation_owner(values, state, shared)
        shared.write_json(directory / "before.tfstate", state)
        shared.write_json(directory / "inputs.tfvars.json", values)
        run_tf(
            args.terraform_binary,
            module,
            directory,
            "plan",
            "plan",
            "-destroy",
            "-input=false",
            "-lock-timeout=60s",
            "-var-file=" + str(directory / "inputs.tfvars.json"),
            "-out=" + str(plan_path),
        )
        plan = json.loads(
            run_tf(
                args.terraform_binary,
                module,
                directory,
                "show",
                "show",
                "-json",
                plan_path,
            )
        )
        rows = validate_plan(plan, state, declarations(source), args.account, shared)
        shared.write_json(directory / "destroy.json", plan)
        manifest = dict(
            target,
            plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
            plan_json_sha256=hashlib.sha256(
                (directory / "destroy.json").read_bytes()
            ).hexdigest(),
            inputs_sha256=hashlib.sha256(
                (directory / "inputs.tfvars.json").read_bytes()
            ).hexdigest(),
            module_sha256=source_digest(module),
            lineage=state.get("lineage"),
            serial=state.get("serial"),
            deletions=[
                {
                    "address": r["address"],
                    "type": r["type"],
                    "id": r["change"]["before"].get("id"),
                }
                for r in rows
            ],
        )
        shared.write_json(manifest_path, manifest)
        print(
            f"Prepared {len(rows)} deletions for {args.kind}; review private {manifest_path}",
            flush=True,
        )
        return 0
    manifest = json.loads(manifest_path.read_text())
    require(
        all(manifest.get(k) == v for k, v in target.items()),
        "Saved proposal target differs",
    )
    require(
        manifest["plan_sha256"] == hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        "Saved plan changed",
    )
    require(
        manifest["module_sha256"] == source_digest(module), "Prepared module changed"
    )
    require(
        manifest["inputs_sha256"]
        == hashlib.sha256((directory / "inputs.tfvars.json").read_bytes()).hexdigest(),
        "Saved inputs changed",
    )
    require(
        json.loads((directory / "inputs.tfvars.json").read_text()) == values,
        "Selected inputs changed",
    )
    plan = json.loads(
        run_tf(
            args.terraform_binary,
            module,
            directory,
            "apply-show",
            "show",
            "-json",
            plan_path,
        )
    )
    require(
        plan == json.loads((directory / "destroy.json").read_text()),
        "Saved rendering changed",
    )
    state = json.loads(
        run_tf(args.terraform_binary, module, directory, "apply-state", "state", "pull")
    )
    require(
        state.get("lineage") == manifest["lineage"]
        and state.get("serial") == manifest["serial"],
        "State changed; prepare a new proposal",
    )
    rows = validate_plan(
        plan,
        state,
        declarations(APP / "infra" / KINDS[args.kind][0]),
        args.account,
        shared,
    )
    if args.kind == "lifecycle-foundations":
        validate_foundation_owner(values, state, shared)
    verify_management(shared, args, directory)
    ensure_quiescent(args.kubeconfig, args.installation_id, args.namespace)
    shared.write_json(directory / "execution.json", dict(target, status="running"))
    run_tf(
        args.terraform_binary,
        module,
        directory,
        "apply",
        "apply",
        "-input=false",
        "-lock-timeout=60s",
        plan_path,
    )
    after = json.loads(
        run_tf(args.terraform_binary, module, directory, "state-after", "state", "pull")
    )
    shared.write_json(directory / "after.tfstate", after)
    require(not list(shared.instances(after)), "Managed resources remain after apply")
    # Use common state-scoped verification and immediate-reinstall secret purge.
    verifier = shared.Run(
        ROOT, args.account, args.region, args.environment, purge_deleted_secrets=True
    )
    verifier.directory = directory
    verifier.record_deletions(args.kind, rows)
    verifier.verify(args.kind)
    shared.write_json(directory / "execution.json", dict(target, status="complete"))
    print(
        f"Removed {len(rows)} {args.kind} resources; private verification retained",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
