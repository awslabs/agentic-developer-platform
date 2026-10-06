"""Separate runtime-plan preparation and independently approved saved-plan apply."""

import argparse
import json
from pathlib import Path

from . import runtime_preparation
from .config import Refusal, deployment_identity, load, require
from .runner import Commands, atomic, local_lock
from .runtime_approval import (
    GitHubPlanApproval,
    decode_json,
    manifest,
    manifest_path,
    private_bytes,
)


class RuntimeInspector:
    """Read-only AWS transport using the same selected process identity as Terraform."""

    def __init__(self, region, commands):
        self.region, self.commands = region, commands

    def aws(self, service, operation, *args):
        require(
            operation.startswith(("get-", "describe-", "list-")),
            "Runtime inspector only accepts read operations",
        )
        return self.commands.call(
            [
                "aws",
                "--region",
                self.region,
                "--no-cli-pager",
                service,
                operation,
                *args,
                "--output",
                "json",
            ],
            timeout=120,
        )

    @staticmethod
    def json(response):
        try:
            return json.loads(response.stdout)
        except (TypeError, ValueError):
            raise Refusal("Runtime AWS response is invalid") from None


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--prepare-domain-runtime", required=True, action="store_true")
    result.add_argument("--environment", required=True, type=Path)
    result.add_argument("--release-lock", required=True, type=Path)
    result.add_argument("--runtime-operator", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--resume", action="store_true")
    result.add_argument("--execute", action="store_true")
    result.add_argument("--approved-plan-sha256")
    result.add_argument(
        "--plan-review", help="Independently approved aws-e/adp plan PR URL"
    )
    return result


def run(args, *, commands=None, approval_api=None):
    require(
        args.execute or not (args.approved_plan_sha256 or args.plan_review),
        "Approval inputs apply only with --execute",
    )
    require(
        not args.execute
        or args.resume
        and args.approved_plan_sha256
        and args.plan_review,
        "Runtime apply requires --resume, --approved-plan-sha256 and --plan-review",
    )
    require(not args.output.is_symlink(), "Runtime output cannot be a symlink")
    directory = args.output.absolute()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    require(directory.stat().st_mode & 0o077 == 0, "Runtime output must be private")
    environment, lock, operator = (
        load(args.environment),
        load(args.release_lock),
        load(args.runtime_operator),
    )
    selected = deployment_identity(environment, required=True)
    request = {
        **{
            key: environment[key]
            for key in ("account_id", "region", "environment", "cluster", "namespace")
        },
        "operator_role_arn": selected["expected_role_arn"],
    }
    commands = commands or Commands()
    inspector = RuntimeInspector(environment["region"], commands)
    with local_lock(directory):
        receipt_path = directory / "runtime-preparation.json"
        require(
            args.resume == receipt_path.exists(),
            "Runtime output exists; use --resume, or select a new private directory",
        )
        approval = None
        if args.resume:
            before = decode_json(private_bytes(receipt_path))
            require(
                "binary_plan_sha256" in before,
                "Runtime source must preserve the reviewed binary plan; legacy receipt needs a fresh plan",
            )
            expected = manifest(environment, operator, directory)
            require(
                decode_json(private_bytes(directory / "runtime-plan-manifest.json"))
                == expected,
                "Saved runtime review manifest differs; obtain fresh independent review",
            )
            if args.execute:
                require(
                    args.approved_plan_sha256 == before["plan_sha256"],
                    "Approved digest differs from saved runtime plan",
                )
                approval = GitHubPlanApproval(
                    args.plan_review, environment, operator, directory, api=approval_api
                )
                # Authenticate before any resumed Terraform work, then the
                # preparation hook authenticates again immediately before apply.
                approval.verify_plan(
                    plan_sha256=before["plan_sha256"],
                    installation_id=before["installation_id"],
                    review_id=before["review_id"],
                )
        receipt = runtime_preparation.prepare(
            request,
            environment,
            lock,
            operator,
            inspector,
            commands,
            directory,
            approved_plan_digest=args.approved_plan_sha256 if args.execute else None,
            approval_check=approval,
        )
        proposed = manifest(environment, operator, directory)
        if not args.resume:
            atomic(directory / "runtime-plan-manifest.json", proposed)
        return {
            "status": receipt["status"],
            "receipt": str(receipt_path),
            "plan_sha256": receipt["plan_sha256"],
            "binary_plan_sha256": proposed["binary_plan_sha256"],
            "manifest": str(directory / "runtime-plan-manifest.json"),
            "review_manifest_path": manifest_path(operator["review_id"]),
            "worker_ready": False,
            "apply_supported": "binary_plan_sha256" in receipt,
        }


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        print(json.dumps(run(args)))
        return 0
    except Refusal as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "Runtime preparation failed; inspect private receipts. No completion claimed.",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
