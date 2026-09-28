"""Apply a reviewed workspace artifact through the complete guard, without replanning."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_workspace_plan as guard
from workspace_ownership import WorkspaceOwnershipError
from workspace_backend import verify_backend
from workspace_kms import (
    verify_account_prerequisites,
    verify_supplied_key,
    verify_provisioning_principal,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "plan-json",
        "plan-file",
        "authorization",
        "inventory",
        "estimate",
        "module-dir",
    ):
        parser.add_argument(f"--{name}", type=Path, required=True)
    for name in (
        "account-id",
        "aws-region",
        "environment",
        "workspace-name",
        "org-id",
        "workspace-id",
    ):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--terraform-binary", default="terraform")
    args = parser.parse_args(argv)

    try:
        if any(name.startswith("TF_CLI_ARGS") for name in os.environ):
            raise WorkspaceOwnershipError(
                "Implicit Terraform CLI arguments are unsupported"
            )
        # Snapshot all reviewed inputs once. Terraform receives this private copy, so replacing
        # the caller's path after validation cannot replace the artifact passed to apply.
        artifact = args.plan_file.read_bytes()
        document = guard._load_authorization(args.authorization)
        expected = document["plan_file_sha256"]
        if hashlib.sha256(artifact).hexdigest() != expected:
            raise WorkspaceOwnershipError(
                "saved artifact differs from the authorization"
            )
        rendered = args.plan_json.read_bytes()
        with tempfile.TemporaryDirectory(
            prefix="superplane-reviewed-apply-"
        ) as directory:
            root = Path(directory)
            saved = root / "reviewed.tfplan"
            plan_json = root / "reviewed.json"
            authorization = root / "authorization.json"
            for path, content in (
                (saved, artifact),
                (plan_json, rendered),
                (authorization, json.dumps(document).encode()),
            ):
                path.write_bytes(content)
                path.chmod(0o400)
            result = guard.main(
                [
                    "--plan-file",
                    str(saved),
                    "--plan-json",
                    str(plan_json),
                    "--authorize-destroy",
                    str(authorization),
                    "--inventory",
                    str(args.inventory.resolve()),
                    "--estimate",
                    str(args.estimate.resolve()),
                    "--module-dir",
                    str(args.module_dir.resolve()),
                    "--terraform-binary",
                    args.terraform_binary,
                    "--account-id",
                    args.account_id,
                    "--aws-region",
                    args.aws_region,
                    "--environment",
                    args.environment,
                    "--workspace-name",
                    args.workspace_name,
                    "--org-id",
                    args.org_id,
                    "--workspace-id",
                    args.workspace_id,
                ]
            )
            if result:
                return result
            plan = json.loads(rendered)
            target = {
                "account_id": args.account_id,
                "aws_region": args.aws_region,
                "environment": args.environment,
                "workspace_name": args.workspace_name,
                "org_id": args.org_id,
                "workspace_id": args.workspace_id,
            }
            backend = verify_backend(saved, plan, args.module_dir.resolve(), target)
            if document.get("backend") != backend:
                raise WorkspaceOwnershipError(
                    "Authorization lacks the exact reviewed backend; use prepare_workspace_plan.py"
                )
            account_prerequisites = verify_account_prerequisites(target)
            if document.get("account_prerequisites") != account_prerequisites:
                raise WorkspaceOwnershipError(
                    "Authorization lacks the verified account prerequisites; prepare again"
                )
            supplied = plan.get("variables", {}).get("kms_key_arn", {}).get("value", "")
            if supplied and not isinstance(document.get("supplied_kms"), dict):
                raise WorkspaceOwnershipError(
                    "Supplied-key authorization lacks mandatory verified preflight evidence"
                )
            kms = verify_supplied_key(plan, approved=document.get("supplied_kms"))
            if (
                verify_backend(saved, plan, args.module_dir.resolve(), target)
                != backend
            ):
                raise WorkspaceOwnershipError("Backend changed during preflight")
            if not isinstance(document.get("provisioning_principal"), dict):
                raise WorkspaceOwnershipError(
                    "Authorization lacks verified provisioning principal; prepare again"
                )
            principal = verify_provisioning_principal(
                plan, approved=document["provisioning_principal"]
            )
            args.inventory.with_name("workspace-apply-preflight.json").write_text(
                json.dumps(
                    {
                        "backend": backend,
                        "account_prerequisites": account_prerequisites,
                        "supplied_kms": kms,
                        "provisioning_principal": principal,
                        "plan_file_sha256": expected,
                    },
                    indent=2,
                )
            )
            # This is deliberately after every verification and immediately before invocation.
            if hashlib.sha256(saved.read_bytes()).hexdigest() != expected:
                raise WorkspaceOwnershipError(
                    "saved artifact changed during verification"
                )
            completed = subprocess.run(
                [args.terraform_binary, "apply", "-input=false", str(saved)],
                cwd=args.module_dir.resolve(),
                check=False,
            )
            return completed.returncode
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        WorkspaceOwnershipError,
    ) as exc:
        print(f"DENIED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
