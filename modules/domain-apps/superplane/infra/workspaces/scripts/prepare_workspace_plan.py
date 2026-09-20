"""Initialize the selected workspace backend and prepare an artifact for review."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import check_workspace_plan as guard
from workspace_backend import initialized_backend, verify_backend
from workspace_ownership import WorkspaceOwnershipError
from workspace_identity import state_key
from workspace_kms import (
    verify_account_prerequisites,
    verify_supplied_key,
    verify_provisioning_principal,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("module-dir", "variables", "output-dir"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("backend-bucket", "backend-region", "lock-table"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--terraform-binary", default="terraform")
    parser.add_argument("--destroy", action="store_true")
    args = parser.parse_args(argv)
    try:
        if any(name.startswith("TF_CLI_ARGS") for name in os.environ):
            raise WorkspaceOwnershipError(
                "Implicit Terraform CLI arguments are unsupported"
            )
        values = json.loads(args.variables.read_text())
        target = {
            name: values[name]
            for name in (
                "account_id",
                "aws_region",
                "environment",
                "workspace_name",
                "org_id",
                "workspace_id",
            )
        }
        if not re.fullmatch(r"[0-9]{12}", target["account_id"]) or any(
            not re.fullmatch(r"[a-z0-9][a-z0-9-]*", target[name])
            for name in ("environment", "workspace_name", "aws_region")
        ):
            raise WorkspaceOwnershipError("Invalid backend target identity")
        module = args.module_dir.resolve()
        version = json.loads(
            subprocess.check_output(
                [args.terraform_binary, "version", "-json"], text=True
            )
        )
        if version.get("terraform_version") != "1.9.8":
            raise WorkspaceOwnershipError(
                "Use the maintained Terraform 1.9.8 executable"
            )
        config = {
            "bucket": args.backend_bucket,
            "region": args.backend_region,
            "dynamodb_table": args.lock_table,
            "key": state_key(target),
            "encrypt": True,
        }
        expected = {"type": "s3", "workspace": "default", **config}
        if (module / "terraform.tfstate").exists():
            raise WorkspaceOwnershipError(
                "Local state must not be migrated or adopted by this command"
            )
        if (module / ".terraform/terraform.tfstate").exists():
            if initialized_backend(module, target) != expected:
                raise WorkspaceOwnershipError(
                    "This module directory is initialized for a different backend; use a fresh directory"
                )
        elif (
            os.environ.get("TF_DATA_DIR")
            or os.environ.get("TF_WORKSPACE", "default") != "default"
        ):
            raise WorkspaceOwnershipError(
                "Custom Terraform data directories/workspaces are unsupported"
            )
        workspace_file = module / ".terraform/environment"
        if workspace_file.exists() and workspace_file.read_text().strip() != "default":
            raise WorkspaceOwnershipError(
                "A reused non-default Terraform workspace is unsupported"
            )
        output = args.output_dir.resolve()
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(output.iterdir()):
            raise WorkspaceOwnershipError(
                "Review output directory must be empty; preserve previous evidence"
            )
        plan_file = output / "workspace.tfplan"
        if plan_file.exists():
            raise WorkspaceOwnershipError(
                "Output already contains a plan; preserve it and choose a new review directory"
            )
        account_prerequisites = verify_account_prerequisites(target)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as backend_file:
            json.dump(config, backend_file)
            backend_file.flush()
            subprocess.run(
                [
                    args.terraform_binary,
                    "init",
                    "-input=false",
                    "-backend-config=" + backend_file.name,
                ],
                cwd=module,
                check=True,
            )
        if initialized_backend(module, target) != expected:
            raise WorkspaceOwnershipError(
                "Initialized backend does not match the requested target"
            )
        command = [
            args.terraform_binary,
            "plan",
            "-input=false",
            "-lock-timeout=60s",
            "-out=" + str(plan_file),
            "-var-file=" + str(args.variables.resolve()),
        ]
        if args.destroy:
            command.append("-destroy")
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".tfvars.json"
        ) as variables_file:
            json.dump(values, variables_file)
            variables_file.flush()
            command = [arg for arg in command if not arg.startswith("-var-file=")]
            command.append("-var-file=" + variables_file.name)
            subprocess.run(command, cwd=module, check=True)
        rendering = subprocess.check_output(
            [args.terraform_binary, "show", "-json", str(plan_file)], cwd=module
        )
        plan = json.loads(rendering)
        backend = verify_backend(plan_file, plan, module, target)
        plan_json = output / "workspace-plan.json"
        plan_json.write_bytes(rendering)
        authorization = output / "workspace-authorization.proposed.json"
        flags = [
            "--plan-file",
            str(plan_file),
            "--plan-json",
            str(plan_json),
            "--module-dir",
            str(module),
            "--terraform-binary",
            args.terraform_binary,
            "--inventory",
            str(output / "workspace-inventory.json"),
            "--estimate",
            str(output / "workspace-estimate.json"),
            "--emit-authorization",
            str(authorization),
        ]
        flags += [
            part
            for name, value in target.items()
            for part in ("--" + name.replace("_", "-"), value)
        ]
        if args.destroy:
            flags.append("--expect-destroy")
        result = guard.main(flags)
        if authorization.exists():
            document = json.loads(authorization.read_text())
            document["backend"] = backend
            document["account_prerequisites"] = account_prerequisites
            document["supplied_kms"] = verify_supplied_key(plan)
            document["provisioning_principal"] = verify_provisioning_principal(plan)
            authorization.write_text(json.dumps(document, indent=2) + "\n")
            (output / "workspace-backend.json").write_text(
                json.dumps(backend, indent=2) + "\n"
            )
        return result
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        WorkspaceOwnershipError,
    ) as exc:
        print(f"DENIED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
