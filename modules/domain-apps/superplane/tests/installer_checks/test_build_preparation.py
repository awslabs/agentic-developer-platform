import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from installation.build_preparation import (
    BuildPreparation,
    ECR_ADDRESSES,
    PAID_ADDRESSES,
    inspect,
)
from installation.config import Refusal
from installation.terraform_sources import stage_control_plane


def plans(env):
    name = f"adp-{env}-superplane-paid-worker"
    result = {}
    values = {
        'aws_ecr_repository.superplane["adp-superplane-paid-worker"]': {
            "name": "adp-superplane-paid-worker",
            "image_tag_mutability": "IMMUTABLE",
        },
        'aws_ecr_lifecycle_policy.superplane["adp-superplane-paid-worker"]': {
            "repository": "adp-superplane-paid-worker"
        },
        "terraform_data.ecr_inventory_guard": {},
        'aws_cloudwatch_log_group.build["paid"]': {"name": "/aws/codebuild/" + name},
        'aws_iam_policy.boundary["paid"]': {
            "name": f"adp-{env}-superplane-paid-build-boundary"
        },
        'aws_iam_role.build["paid"]': {
            "name": f"adp-{env}-codebuild-superplane-paid-worker"
        },
        'aws_iam_role_policy.build["paid"]': {"name": "build-scope"},
        'aws_codebuild_project.build["paid"]': {"name": name},
    }
    for stage, addresses in (("ecr", ECR_ADDRESSES), ("paid-build", PAID_ADDRESSES)):
        result[stage] = {
            "resource_changes": [
                {
                    "mode": "managed",
                    "address": a,
                    "type": a.split(".")[0],
                    "change": {"actions": ["create"], "after": values[a]},
                }
                for a in sorted(addresses)
            ]
        }
    return result


class Transport:
    def __init__(self, environment, release):
        self.environment = environment
        self.release = release
        self.plans = plans(environment["environment"])
        self.calls = []
        self.applied = []
        self.remote_lock = None
        self.collision = False
        self.switch_after_ecr = False

    def call(self, args, **kwargs):
        self.calls.append(args)
        output, error, rc = {}, "", 0
        if args[0] == "git":
            output = self.release["source_revision"] if "rev-parse" in args else ""
        elif args[0] == "terraform":
            directory = Path(args[1].split("=", 1)[1])
            if "plan" in args:
                (directory / "preparation.tfplan").write_bytes(
                    b"saved-plan-" + directory.name.encode()
                )
            if "show" in args:
                output = self.plans[directory.name]
            if "apply" in args:
                self.applied.append(directory.name)
        elif "get-caller-identity" in args:
            selected = self.environment["deployment_identity"]
            name = selected["expected_role_arn"].rsplit("/", 1)[1]
            if self.switch_after_ecr and self.applied:
                name = "different-role"
            output = {
                "Account": self.environment["account_id"],
                "Arn": f"arn:aws:sts::{self.environment['account_id']}:assumed-role/{name}/test",
                "UserId": selected["expected_role_id"] + ":test",
            }
        elif (
            "get-role" in args
            and args[args.index("--role-name") + 1]
            == self.environment["deployment_identity"]["expected_role_arn"].rsplit(
                "/", 1
            )[1]
        ):
            selected = self.environment["deployment_identity"]
            output = {
                "Role": {
                    "Arn": selected["expected_role_arn"],
                    "RoleId": selected["expected_role_id"],
                }
            }
        elif "get-role" in args or "get-policy" in args or "get-role-policy" in args:
            error, rc = "NoSuchEntity", 254
        elif "describe-repositories" in args:
            if self.collision:
                output = {
                    "repositories": [{"repositoryName": "adp-superplane-paid-worker"}]
                }
            else:
                error, rc = "RepositoryNotFoundException", 254
        elif "batch-get-projects" in args:
            output = {
                "projects": [],
                "projectsNotFound": [
                    f"adp-{self.environment['environment']}-superplane-paid-worker"
                ],
            }
        elif "describe-log-groups" in args:
            output = {"logGroups": []}
        elif "put-object" in args:
            self.remote_lock = json.loads(
                Path(args[args.index("--body") + 1]).read_text()
            )
            output = {"ETag": '"exclusive"'}
        elif "get-object" in args:
            if self.remote_lock is None:
                error, rc = "NoSuchKey", 254
            else:
                Path(args[-1]).write_text(json.dumps(self.remote_lock))
                output = {"ETag": '"exclusive"'}
        elif "delete-object" in args:
            self.remote_lock = None
        else:
            raise AssertionError(args)
        return SimpleNamespace(
            stdout=output if isinstance(output, str) else json.dumps(output),
            stderr=error,
            returncode=rc,
        )


def preparation(tmp_path, environment, release):
    release["pending_images"] = {
        "superplane-paid-worker": {"ecr_repository": "adp-superplane-paid-worker"}
    }
    transport = Transport(environment, release)
    instance = BuildPreparation(environment, release, tmp_path, transport)
    instance.plan()
    return instance, transport


def test_offline_snapshot_resolves_all_local_module_and_manifest_inputs(
    tmp_path, environment, release
):
    instance, transport = preparation(tmp_path, environment, release)
    assert transport.calls == []
    root = tmp_path / "ecr"
    assert yaml.safe_load((root / "release-lock.yaml").read_text()) == release
    assert (root / "dependencies/image-builds/main.tf").is_file()
    assert (root / "dependencies/projects.json").is_file()
    assert (
        'source                     = "./dependencies/image-builds"'
        in (root / "main.tf").read_text()
    )
    assert (
        json.loads((root / "installation.auto.tfvars.json").read_text())[
            "manage_image_builds"
        ]
        is False
    )
    assert (
        instance.lock_key
        == f"{environment['environment']}/modules/superplane/installation.lock"
    )
    assert (tmp_path / "paid-build/paid-worker-project.json").is_file()


def test_supplied_release_inventory_overrides_checkout_default(tmp_path, release):
    release["pending_images"] = {
        "new-image": {"ecr_repository": "adp-superplane-reviewed-input"}
    }
    stage_control_plane(tmp_path, release)
    assert (
        "adp-superplane-reviewed-input" in (tmp_path / "release-lock.yaml").read_text()
    )


def test_preflight_and_approved_apply_are_bounded_and_separate(
    tmp_path, environment, release
):
    instance, tools = preparation(tmp_path, environment, release)
    instance.preflight()
    initialization = [
        call for call in tools.calls if call[0] == "terraform" and "init" in call
    ]
    assert len(initialization) == 2
    assert any(
        "-backend-config=key=dev/modules/superplane/terraform.tfstate" in call
        for call in initialization
    )
    assert any(
        "-backend-config=key=dev/modules/superplane/paid-worker-build/terraform.tfstate"
        in call
        for call in initialization
    )
    assert tools.applied == []
    instance.execute(instance.receipt["plan_sha256"])
    assert tools.applied == ["ecr", "paid-build"]
    assert tools.remote_lock is None
    assert instance.receipt["status"] == "build-infrastructure-prepared"
    assert not any("start-build" in call or "kubectl" in call for call in tools.calls)


@pytest.mark.parametrize("actions", [["update"], ["delete"], ["create", "delete"]])
def test_any_modification_of_existing_build_resources_is_refused(actions):
    plan = plans("dev")["paid-build"]
    plan["resource_changes"][0]["change"]["actions"] = actions
    with pytest.raises(Refusal, match="additive"):
        inspect(plan, "paid-build", "dev")


def test_unrelated_lane_creation_and_incomplete_plan_refused():
    plan = plans("dev")["paid-build"]
    plan["resource_changes"].append(
        {
            "mode": "managed",
            "address": 'module.image_builds.aws_codebuild_project.main["superplane-api"]',
            "change": {"actions": ["create"]},
        }
    )
    with pytest.raises(Refusal, match="unrelated"):
        inspect(plan, "paid-build", "dev")
    with pytest.raises(Refusal, match="incomplete"):
        inspect({"resource_changes": []}, "paid-build", "dev")


@pytest.mark.parametrize("alter", ["source", "binary", "receipt", "wrong-approval"])
def test_approval_cannot_apply_changed_inputs(tmp_path, environment, release, alter):
    instance, tools = preparation(tmp_path, environment, release)
    instance.preflight()
    approved = instance.receipt["plan_sha256"]
    if alter == "source":
        (tmp_path / "paid-build/main.tf").write_text("changed")
    elif alter == "binary":
        (tmp_path / "paid-build/preparation.tfplan").write_text("changed")
    elif alter == "receipt":
        instance.receipt["plans"]["paid-build"]["backend_key"] = "different/state"
    else:
        approved = "0" * 64
    with pytest.raises(Refusal):
        instance.execute(approved)
    assert tools.applied == []


def test_existing_unowned_repository_is_not_adopted(tmp_path, environment, release):
    instance, tools = preparation(tmp_path, environment, release)
    tools.collision = True
    with pytest.raises(Refusal, match="outside the selected state"):
        instance.preflight()
    assert tools.applied == []


def test_refreshed_credentials_are_checked_between_independent_applies(
    tmp_path, environment, release
):
    instance, tools = preparation(tmp_path, environment, release)
    instance.preflight()
    tools.switch_after_ecr = True
    with pytest.raises(Refusal, match="selected connection"):
        instance.execute(instance.receipt["plan_sha256"])
    assert tools.applied == ["ecr"]
    assert tools.remote_lock is not None
    assert instance.receipt["status"] == "recovery-required"
    with pytest.raises(Refusal):
        instance.execute(instance.receipt["plan_sha256"])


def test_resume_refuses_different_target(tmp_path, environment, release):
    instance, tools = preparation(tmp_path, environment, release)
    altered = copy.deepcopy(environment)
    altered["cluster"] = "other-cluster"
    with pytest.raises(Refusal, match="inputs differ"):
        BuildPreparation(altered, release, tmp_path, tools, previous=instance.receipt)


def test_failed_apply_lock_requires_stopped_confirmation_and_replan(
    tmp_path, environment, release
):
    instance, tools = preparation(tmp_path, environment, release)
    instance.preflight()
    tools.switch_after_ecr = True
    with pytest.raises(Refusal):
        instance.execute(instance.receipt["plan_sha256"])
    tools.switch_after_ecr = False
    with pytest.raises(Refusal, match="Confirm"):
        instance.recover_lock("different-run")
    assert tools.remote_lock is not None
    instance.recover_lock(instance.run_id)
    assert tools.remote_lock is None
    assert instance.receipt["status"] == "recovered-replan-required"
    assert "plan_sha256" not in instance.receipt
    with pytest.raises(Refusal):
        instance.execute("old-approval")


def test_cli_offline_mode_accepts_pending_image_without_aws_tools(
    tmp_path, environment, release
):
    from installation.config import MODULE

    release["pending_images"] = {
        "superplane-paid-worker": {"ecr_repository": "adp-superplane-paid-worker"}
    }
    environment_path, lock_path = (
        tmp_path / "environment.yaml",
        tmp_path / "release.yaml",
    )
    environment_path.write_text(yaml.safe_dump(environment))
    lock_path.write_text(yaml.safe_dump(release))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "installation",
            "--prepare-paid-build",
            "--environment",
            str(environment_path),
            "--release-lock",
            str(lock_path),
            "--output",
            str(tmp_path / "output"),
        ],
        cwd=MODULE,
        env={**os.environ, "PATH": "", "PYTHONPATH": str(MODULE)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout)["status"] == "planned-offline"
