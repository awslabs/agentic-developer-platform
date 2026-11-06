"""Offline executor image publisher and installed readback controls."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "modules/L5-services/scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("validation_images_release", SCRIPTS / "release-validation-images.py")
images_release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images_release)

SOURCE = "a" * 40
REGISTRY = "123456789012.dkr.ecr.us-east-1.amazonaws.com"


@pytest.fixture
def selected():
    environment = {
        "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": "example/project", "GITHUB_REPOSITORY_ID": "123",
        "GITHUB_WORKFLOW_REF": "example/project/.github/workflows/l5-services-release.yml@refs/heads/main",
        "GITHUB_RUN_ID": "456", "GITHUB_RUN_ATTEMPT": "1", "GITHUB_WORKFLOW_SHA": "b" * 40,
        "GITHUB_SHA": "b" * 40, "L5_CHECKOUT_REVISION": SOURCE,
        "L5_SERVICE": "transform", "L5_ENVIRONMENT": "dev",
        "REVIEWED_SOURCE_SHA": SOURCE, "AWS_REGION": "us-east-1",
        "L5_RELEASE_ROLE_ARN": "arn:aws:iam::123456789012:role/release",
        "L5_MUTATION_ROLE_ARN": "arn:aws:iam::123456789012:role/deploy",
        "ADP_VALIDATION_SERVICE_REPOSITORY": REGISTRY + "/validation-service",
        "ADP_VALIDATION_CHECK_REPOSITORY": REGISTRY + "/validation-check",
        "ADP_VALIDATION_FUNCTION_NAME": "example-validation-service",
        "ADP_VALIDATION_NAMESPACE": "example-validation",
        "ADP_VALIDATION_CHECK_POD": "example-check-pod",
    }
    identity = images_release.release_identity(environment)
    artifacts = {**identity, "artifacts": {"wheel": {"sha256": "c" * 64},
                                                 "archive": {"sha256": "d" * 64}}}
    return environment, identity, artifacts


class Runner:
    def __init__(self):
        self.commands = []
        self.mutability = "IMMUTABLE"
        self.function = None
        self.pod = None

    def __call__(self, command, *, input=None):
        self.commands.append(command)
        if command[1:3] == ["sts", "get-caller-identity"]:
            return json.dumps({"Account": "123456789012", "Arn":
                "arn:aws:sts::123456789012:assumed-role/deploy/example"}).encode()
        if command[1:3] == ["ecr", "describe-repositories"]:
            repository = command[command.index("--repository-names") + 1]
            return json.dumps({"repositories": [{"repositoryUri": REGISTRY + "/" + repository,
                                                   "imageTagMutability": self.mutability}]}).encode()
        if command[1:3] == ["ecr", "describe-images"]:
            return json.dumps({"imageDetails": [{"imageDigest": "sha256:" + "e" * 64}]}).encode()
        if command[1:3] == ["ecr", "get-login-password"]:
            return b"not-a-real-password\n"
        if command[1:3] == ["lambda", "get-function"]:
            return json.dumps({"Code": {"ResolvedImageUri": self.function}}).encode()
        if command[:3] == ["kubectl", "get", "pod"]:
            return json.dumps(self.pod).encode()
        assert command[0] == "docker"
        if command[1] == "login":
            assert input == b"not-a-real-password\n"
        return b""


def test_publisher_requires_selected_immutable_repositories_and_retains_digests(selected):
    environment, identity, artifacts = selected
    runner = Runner()
    result = images_release.publish(identity, artifacts, environment, run=runner)
    assert result["phase"] == "artifacts"
    assert result["source_revision"] == SOURCE
    assert result["wheel_sha256"] == "c" * 64
    assert result["images"] == {
        label: environment[key] + "@sha256:" + "e" * 64
        for label, (key, _) in images_release.IMAGES.items()
    }
    assert len([command for command in runner.commands if command[:2] == ["docker", "build"]]) == 2
    assert all("--provenance=false" in command for command in runner.commands if command[:2] == ["docker", "build"])
    assert len([command for command in runner.commands if command[:2] == ["aws", "ecr"]
                and command[2] == "describe-repositories"]) == 2
    assert not {"installed", "qualification", "checks_passed"} & result.keys()


@pytest.mark.parametrize("change", ["wrong-target", "mutable", "wrong-attempt", "wrong-service"])
def test_publisher_refuses_unsafe_inputs_before_docker(selected, change):
    environment, identity, artifacts = selected
    runner = Runner()
    if change == "wrong-target":
        environment["ADP_VALIDATION_CHECK_REPOSITORY"] = "000000000002.dkr.ecr.us-east-1.amazonaws.com/check"
    elif change == "mutable":
        runner.mutability = "MUTABLE"
    elif change == "wrong-attempt":
        artifacts["source_revision"] = "f" * 40
    else:
        environment["L5_SERVICE"] = "security"
    with pytest.raises(ValueError):
        images_release.publish(identity, artifacts, environment, run=runner)
    assert not any(command[0] == "docker" for command in runner.commands)


def test_installed_readback_requires_actual_lambda_and_pod_image_identity(selected):
    environment, identity, artifacts = selected
    runner = Runner()
    manifest = images_release.publish(identity, artifacts, environment, run=runner)
    runner.function = manifest["images"]["service"]
    runner.pod = {"metadata": {"name": "example-check-pod", "namespace": "example-validation", "uid": "pod-uid"},
                  "spec": {"containers": [{"image": manifest["images"]["checks"]}]},
                  "status": {"containerStatuses": [{"imageID": "docker-pullable://" + manifest["images"]["checks"]}]}}
    assert images_release.installed(manifest, environment, run=runner)["check_pod_uid"] == "pod-uid"
    runner.function = environment["ADP_VALIDATION_SERVICE_REPOSITORY"] + "@sha256:" + "f" * 64
    with pytest.raises(ValueError, match="Installed validation service"):
        images_release.installed(manifest, environment, run=runner)
    runner.function = manifest["images"]["service"]
    runner.pod["status"]["containerStatuses"][0]["imageID"] = "docker-pullable://other@sha256:" + "f" * 64
    with pytest.raises(ValueError, match="Observed validation check Pod"):
        images_release.installed(manifest, environment, run=runner)




def test_installed_readback_rejects_foreign_registry_and_identity(selected):
    environment, identity, artifacts = selected
    runner = Runner()
    manifest = images_release.publish(identity, artifacts, environment, run=runner)
    environment["ADP_VALIDATION_CHECK_REPOSITORY"] = "000000000002.dkr.ecr.us-east-1.amazonaws.com/foreign"
    with pytest.raises(ValueError, match="selected AWS target"):
        images_release.installed(manifest, environment, run=runner)
    environment["ADP_VALIDATION_CHECK_REPOSITORY"] = REGISTRY + "/validation-check"
    manifest["images"]["checks"] = environment["ADP_VALIDATION_CHECK_REPOSITORY"] + "@sha256:" + "f" * 64
    with pytest.raises(ValueError, match="registry image digest changed"):
        images_release.installed(manifest, environment, run=runner)


def test_only_transform_release_triggers_executor_publication():
    workflow = yaml.load((ROOT / ".github/workflows/l5-services-release.yml").read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["publish"]["steps"]
    guard = next(index for index, step in enumerate(steps) if "release_context.py" in step.get("run", ""))
    publish = next(index for index, step in enumerate(steps)
                   if "release-transform-images.py --publish" in step.get("run", ""))
    assert guard < publish
    assert steps[publish]["if"] == "inputs.service == 'transform'"
    assert "--check-aws" in steps[publish]["run"]
    assert "validation-images.json" in steps[-1]["with"]["path"]
    assert not any("release-validation-images.py" in step.get("run", "") for step in steps)
    preflight = next(index for index, step in enumerate(steps)
                     if "release-transform-images.py --check-config" in step.get("run", ""))
    package = next(index for index, step in enumerate(steps) if "build-package.sh" in step.get("run", ""))
    assert guard < preflight < package < publish
    assert "$L5_RELEASE_WHEEL" in steps[publish]["run"]
    assert "secrets.ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON" in steps[publish]["env"]["ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON"]
