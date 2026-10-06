"""Explicit, additive preparation of the native paid-worker build infrastructure.

This mode never installs a workload, dispatches a build, or enrolls legacy lanes.
The repository remains in domain state; the five build resources use their own
backend. Every apply uses an inspected saved plan and refreshed selected identity.
"""

import hashlib
import json
import re
import uuid
from pathlib import Path

from .config import MODULE, SHA, deployment_identity, digest, identity, require
from .runner import Commands, Installer, atomic
from .terraform_sources import stage_control_plane, stage_paid_build

REPOSITORY = "adp-superplane-paid-worker"
ECR_ADDRESSES = {
    f'aws_ecr_repository.superplane["{REPOSITORY}"]',
    f'aws_ecr_lifecycle_policy.superplane["{REPOSITORY}"]',
    "terraform_data.ecr_inventory_guard",
}
PAID_ADDRESSES = {
    'aws_cloudwatch_log_group.build["paid"]',
    'aws_iam_policy.boundary["paid"]',
    'aws_iam_role.build["paid"]',
    'aws_iam_role_policy.build["paid"]',
    'aws_codebuild_project.build["paid"]',
}


def inspect(plan, stage, environment):
    """Allow only the exact additive preparation resources; never broad IAM drift."""
    require(
        isinstance(plan.get("resource_changes"), list)
        and not plan.get("errored")
        and not plan.get("deferred_changes"),
        "Incomplete preparation plan",
    )
    expected = ECR_ADDRESSES if stage == "ecr" else PAID_ADDRESSES
    observed = set()
    for resource in plan.get("resource_changes", []):
        require(
            not resource.get("importing")
            and not resource.get("change", {}).get("importing"),
            "Preparation cannot import existing resources",
        )
        actions = resource.get("change", {}).get("actions")
        if resource.get("mode") == "data":
            require(actions in (["read"], ["no-op"]), "Unexpected data-source action")
            continue
        address = resource.get("address")
        if address not in expected:
            require(
                actions == ["no-op"], "Preparation would change unrelated resources"
            )
            continue
        observed.add(address)
        require(
            actions in (["create"], ["no-op"]),
            "Preparation must be additive; update/delete refused",
        )
        after = resource["change"].get("after") or {}
        if stage == "ecr":
            if resource["type"] == "aws_ecr_repository":
                require(
                    after.get("name") == REPOSITORY
                    and after.get("image_tag_mutability") == "IMMUTABLE",
                    "Wrong paid repository contract",
                )
            elif resource["type"] == "aws_ecr_lifecycle_policy":
                require(
                    after.get("repository") == REPOSITORY,
                    "Wrong paid repository lifecycle",
                )
        else:
            names = {
                "aws_cloudwatch_log_group": f"/aws/codebuild/adp-{environment}-superplane-paid-worker",
                "aws_iam_policy": f"adp-{environment}-superplane-paid-build-boundary",
                "aws_iam_role": f"adp-{environment}-codebuild-superplane-paid-worker",
                "aws_iam_role_policy": "build-scope",
                "aws_codebuild_project": f"adp-{environment}-superplane-paid-worker",
            }
            require(
                after.get("name") == names.get(resource["type"]),
                "Wrong paid resource identity",
            )
    require(observed == expected, "Preparation plan is incomplete")


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes(directory):
    return {
        str(path.relative_to(directory)): file_hash(path)
        for path in sorted(directory.rglob("*"))
        if path.is_file()
        and ".terraform" not in path.parts
        and (
            path.suffix in {".tf", ".yaml"}
            or path.name.endswith(".json")
            and path.name != "plan.json"
        )
    }


def maintained_sources():
    paths = [
        *(MODULE / "infra/control-plane").glob("*.tf"),
        *(MODULE / "infra/paid-worker-build").glob("*.tf"),
        *(MODULE.parent / "shared/infra/codebuild-projects").glob("*.tf"),
        MODULE / "codebuild/projects.json",
        MODULE / "infra/paid-worker-project.json",
    ]
    return {
        str(path.relative_to(MODULE.parent)): file_hash(path) for path in sorted(paths)
    }


class BuildPreparation(Installer):
    """Reuse installer identity, receipt and exclusive-lock guards without rollout."""

    def __init__(self, env, lock, directory, commands=None, previous=None):
        require(
            re.fullmatch(r"[0-9]{12}", str(env.get("account_id", ""))),
            "Explicit account_id required",
        )
        require(
            re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", str(env.get("region", ""))),
            "Explicit region required",
        )
        require(
            re.fullmatch(r"[a-z][a-z0-9]{0,15}", str(env.get("environment", ""))),
            "Bounded environment required",
        )
        require(
            isinstance(env.get("cluster"), str) and env["cluster"],
            "Management cluster identity required",
        )
        require(
            SHA.fullmatch(str(lock.get("source_revision", ""))),
            "Preparation requires exact source_revision",
        )
        deployment_identity(env)
        candidates = {**lock.get("pending_images", {}), **lock.get("image_sources", {})}
        require(
            candidates.get("superplane-paid-worker", {}).get("ecr_repository")
            == REPOSITORY,
            "Lock must retain the paid repository inventory",
        )
        self.env, self.lock, self.directory = env, lock, Path(directory)
        self.commands = commands or Commands()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.bucket = f"adp-terraform-state-{env['account_id']}"
        self.owner = identity(env)
        self.lock_key = f"{env['environment']}/modules/superplane/installation.lock"
        self.receipt_path = self.directory / "receipt.json"
        binding = {"environment": digest(env), "release_lock": digest(lock)}
        if previous:
            require(
                previous.get("mode") == "paid-build-preparation"
                and previous.get("inputs") == binding,
                "Preparation resume inputs differ",
            )
            self.receipt = previous
            self.run_id = previous["run_id"]
        else:
            self.run_id = uuid.uuid4().hex[:16]
            self.receipt = {
                "mode": "paid-build-preparation",
                "inputs": binding,
                "run_id": self.run_id,
                "installation_id": self.owner,
                "status": "planned-offline",
                "completed": [],
            }

    def plan(self):
        require(
            not self.receipt.get("plans"), "Reviewed preparation cannot be rewritten"
        )
        stage_control_plane(self.directory / "ecr", self.lock)
        stage_paid_build(self.directory / "paid-build")
        variables = {
            "environment": self.env["environment"],
            "account_id": self.env["account_id"],
            "aws_region": self.env["region"],
            "namespace": self.env["namespace"],
            "skypilot_namespace": self.env["skypilot_namespace"],
            "database_schema": self.env["database"]["schema"],
            "database_secret_name": self.env["secrets"]["database"],
            "jwt_secret_name": self.env["secrets"]["observation"],
            "cors_allowed_origins": [self.env["origin"]],
            "manage_image_builds": False,
            "workspace_cluster_context": self.env.get("workspace_cluster", ""),
            "api_producer_role": None,
        }
        atomic(self.directory / "ecr/installation.auto.tfvars.json", variables)
        atomic(
            self.directory / "paid-build/installation.auto.tfvars.json",
            {
                "account_id": self.env["account_id"],
                "region": self.env["region"],
                "environment": self.env["environment"],
                "enabled": True,
            },
        )
        self.receipt["sources"] = {
            stage: source_hashes(self.directory / stage)
            for stage in ("ecr", "paid-build")
        }
        self.receipt["maintained_sources"] = maintained_sources()
        self.receipt["actions"] = [
            "review additive paid repository plan in existing domain state",
            "review five dedicated build resources in separate paid-build state",
            "apply exact saved plans after selected-role verification",
            "stop before image build, registry binding or workload activation",
        ]
        self.save()

    def check_sources(self):
        require(
            self.receipt.get("maintained_sources") == maintained_sources(),
            "Maintained preparation source changed; start a fresh preparation output",
        )
        require(
            self.receipt.get("sources")
            == {
                stage: source_hashes(self.directory / stage)
                for stage in ("ecr", "paid-build")
            },
            "Preparation source snapshot changed",
        )

    def terraform_call(self, stage, *args):
        self.verify_deployment_identity()
        return self.commands.call(
            ["terraform", f"-chdir={self.directory / stage}", *args],
            timeout=self.env.get("timeout_seconds", 900),
        )

    def check_new_resource_absence(self, stage, plan):
        """Prevent provider create/upsert from adopting an existing unowned object."""
        creates = {
            r["address"]
            for r in plan.get("resource_changes", [])
            if r.get("mode") == "managed"
            and r.get("change", {}).get("actions") == ["create"]
        }
        if stage == "ecr":
            if f'aws_ecr_repository.superplane["{REPOSITORY}"]' in creates:
                result = self.aws(
                    "ecr",
                    "describe-repositories",
                    "--repository-names",
                    REPOSITORY,
                    allow_failure=True,
                )
                require(
                    result.returncode != 0
                    and "RepositoryNotFoundException" in result.stderr,
                    "Paid repository already exists outside the selected state or cannot be checked",
                )
            elif f'aws_ecr_lifecycle_policy.superplane["{REPOSITORY}"]' in creates:
                result = self.aws(
                    "ecr",
                    "get-lifecycle-policy",
                    "--repository-name",
                    REPOSITORY,
                    allow_failure=True,
                )
                require(
                    result.returncode != 0
                    and "LifecyclePolicyNotFoundException" in result.stderr,
                    "Existing unowned ECR lifecycle policy cannot be adopted",
                )
            return
        env = self.env["environment"]
        checks = {
            'aws_iam_role.build["paid"]': (
                "iam",
                "get-role",
                "--role-name",
                f"adp-{env}-codebuild-superplane-paid-worker",
            ),
            'aws_iam_policy.boundary["paid"]': (
                "iam",
                "get-policy",
                "--policy-arn",
                f"arn:aws:iam::{self.env['account_id']}:policy/adp-{env}-superplane-paid-build-boundary",
            ),
        }
        for address, args in checks.items():
            if address in creates:
                result = self.aws(*args, allow_failure=True)
                require(
                    result.returncode != 0 and "NoSuchEntity" in result.stderr,
                    "Paid IAM resource already exists outside selected state or cannot be checked",
                )
        role_address = 'aws_iam_role.build["paid"]'
        if (
            'aws_iam_role_policy.build["paid"]' in creates
            and role_address not in creates
        ):
            result = self.aws(
                "iam",
                "get-role-policy",
                "--role-name",
                f"adp-{env}-codebuild-superplane-paid-worker",
                "--policy-name",
                "build-scope",
                allow_failure=True,
            )
            require(
                result.returncode != 0 and "NoSuchEntity" in result.stderr,
                "Existing unowned inline policy cannot be adopted",
            )
        if 'aws_codebuild_project.build["paid"]' in creates:
            project = f"adp-{env}-superplane-paid-worker"
            result = self.json(
                self.aws(
                    "codebuild",
                    "batch-get-projects",
                    "--names",
                    project,
                    "--output",
                    "json",
                )
            )
            require(
                result.get("projects") == []
                and result.get("projectsNotFound") == [project],
                "Paid project already exists outside selected state",
            )
        if 'aws_cloudwatch_log_group.build["paid"]' in creates:
            name = f"/aws/codebuild/adp-{env}-superplane-paid-worker"
            result = self.json(
                self.aws(
                    "logs",
                    "describe-log-groups",
                    "--log-group-name-prefix",
                    name,
                    "--output",
                    "json",
                )
            )
            require(
                isinstance(result.get("logGroups"), list)
                and not any(x.get("logGroupName") == name for x in result["logGroups"]),
                "Paid log group already exists outside selected state",
            )

    def preflight(self):
        require(
            not self.receipt.get("remote_lock")
            and not self.receipt.get("lock_attempt"),
            "Reconcile retained installation lock before preparing another plan",
        )
        self.check_sources()
        self.verify_deployment_identity()
        require(
            self.commands.call(
                ["git", "-C", str(MODULE), "rev-parse", "HEAD"]
            ).stdout.strip()
            == self.lock["source_revision"],
            "Checkout differs from preparation source_revision",
        )
        require(
            not self.commands.call(
                [
                    "git",
                    "-C",
                    str(MODULE),
                    "status",
                    "--porcelain",
                    "--untracked-files=normal",
                ]
            ).stdout.strip(),
            "Preparation requires a clean maintained source checkout",
        )
        plans = {}
        for stage in ("ecr", "paid-build"):
            key = f"{self.env['environment']}/modules/superplane/" + (
                "terraform.tfstate"
                if stage == "ecr"
                else "paid-worker-build/terraform.tfstate"
            )
            self.terraform_call(
                stage,
                "init",
                "-input=false",
                f"-backend-config=bucket={self.bucket}",
                f"-backend-config=key={key}",
                f"-backend-config=region={self.env['region']}",
                "-backend-config=encrypt=true",
                "-backend-config=dynamodb_table=adp-terraform-locks",
            )
            targets = (
                ["-target=" + address for address in sorted(ECR_ADDRESSES)]
                if stage == "ecr"
                else []
            )
            self.terraform_call(
                stage, "plan", "-input=false", "-out=preparation.tfplan", *targets
            )
            plan = self.json(
                self.terraform_call(stage, "show", "-json", "preparation.tfplan")
            )
            inspect(plan, stage, self.env["environment"])
            self.check_new_resource_absence(stage, plan)
            atomic(self.directory / stage / "plan.json", plan)
            plans[stage] = {
                "backend_key": key,
                "binary_sha256": file_hash(
                    self.directory / stage / "preparation.tfplan"
                ),
                "json_sha256": file_hash(self.directory / stage / "plan.json"),
            }
        self.receipt["plans"] = plans
        self.receipt["plan_sha256"] = digest(
            {
                "inputs": self.receipt["inputs"],
                "sources": self.receipt["sources"],
                "maintained_sources": self.receipt["maintained_sources"],
                "plans": plans,
            }
        )
        self.receipt["status"] = "preflight-passed"
        self.save()

    def execute(self, approved):
        require(
            self.receipt.get("status") == "preflight-passed"
            and approved
            and approved == self.receipt.get("plan_sha256"),
            "Exact reviewed preparation plan hash required",
        )
        self.check_sources()
        require(
            approved
            == digest(
                {
                    "inputs": self.receipt["inputs"],
                    "sources": self.receipt["sources"],
                    "maintained_sources": self.receipt["maintained_sources"],
                    "plans": self.receipt["plans"],
                }
            ),
            "Preparation receipt no longer matches approved plan",
        )
        for stage, metadata in self.receipt["plans"].items():
            require(
                file_hash(self.directory / stage / "preparation.tfplan")
                == metadata["binary_sha256"]
                and file_hash(self.directory / stage / "plan.json")
                == metadata["json_sha256"],
                "Saved preparation plan changed",
            )
            plan = json.loads((self.directory / stage / "plan.json").read_text())
            inspect(plan, stage, self.env["environment"])
            self.check_new_resource_absence(stage, plan)
        self.verify_deployment_identity()
        with self.exclusive():
            for stage in ("ecr", "paid-build"):
                self.phase(
                    stage,
                    lambda stage=stage: self.terraform_call(
                        stage, "apply", "-input=false", "preparation.tfplan"
                    ),
                )
        self.receipt["status"] = "build-infrastructure-prepared"
        self.receipt["remaining"] = [
            "verify dispatcher grants",
            "build and independently qualify exact merged-source image",
            "prepare runtime binding and native lifecycle activation",
        ]
        self.save()

    def recover_lock(self, confirmed_stopped):
        require(
            confirmed_stopped == self.run_id,
            "Confirm the recorded preparation process and Terraform children have stopped",
        )
        self.verify_deployment_identity()
        require(
            self.receipt.get("lock_attempt"), "No retained preparation lock attempt"
        )
        lock = self.reconcile_lock_attempt()
        if lock is not None:
            self.release_lock(lock)
        else:
            self.receipt.pop("lock_attempt", None)
            self.receipt.pop("remote_lock", None)
        self.receipt["status"] = "recovered-replan-required"
        self.receipt.pop("plan_sha256", None)
        self.save()
