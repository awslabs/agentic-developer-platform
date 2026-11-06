"""Exercise real publication control flow with fake Docker/ECR boundaries."""

import copy
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/L5-services/scripts"))
spec = importlib.util.spec_from_file_location(
    "transform_image_publication", ROOT / "modules/L5-services/scripts/release-transform-images.py")
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)
SOURCE = "a" * 40
REPOSITORY = "adp-dev-l5-transform-executor"
REFERENCE = "123456789012.dkr.ecr.us-east-1.amazonaws.com/" + REPOSITORY


@pytest.fixture
def selected(tmp_path, monkeypatch):
    config = {
        "schema_version": 1,
        "provider_base_image": "registry.example.invalid/python-node@sha256:" + "b" * 64,
        "check_base_image": "registry.example.invalid/tests@sha256:" + "c" * 64,
        "check_policy": {"bundle_sha256": "d" * 64, "lock_sha256": "e" * 64,
                         "oracle_version": "pytest-v1", "commands": [{
                             "argv": ["python", "-m", "pytest", "-q"], "cwd": ".",
                             "environment": {}, "timeout_seconds": 120, "minimum_test_count": 1}]},
    }
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
        "ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON": json.dumps(config),
    }
    wheel = tmp_path / "worker.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in ("provider_runner", "check_runner"):
            archive.writestr(f"l5_services/adapters/transform/{name}.py", "# synthetic wheel\n")
    vendor = tmp_path / "vendor.zip"
    with zipfile.ZipFile(vendor, "w") as archive:
        archive.writestr("atx", b"synthetic vendor")
        archive.writestr("chunk.js", b"synthetic adjacent chunk")
    monkeypatch.setattr(publisher, "VENDOR_SHA256", publisher.digest(vendor.read_bytes()))
    monkeypatch.setattr(publisher, "EXECUTABLE_SHA256", publisher.digest(b"synthetic vendor"))
    identity = publisher.release_identity(environment)
    artifacts = {**identity, "artifacts": {"wheel": {"sha256": publisher.digest(wheel.read_bytes())},
                                          "archive": {"sha256": "f" * 64}}}
    return environment, identity, artifacts, wheel, vendor.read_bytes()


class Runner:
    def __init__(self):
        self.calls, self.builds, self.local, self.registry = [], [], {}, {}
        self.source, self.account, self.mutability = SOURCE, "123456789012", "IMMUTABLE"
        self.registry_fault = None
        self.push_uncertain = False
        self.containerd_store = False
        self.manifests = {}

    def __call__(self, command, *, input=None, env=None, stdout=None):
        if command[:2] == ["docker", "--config"]:
            assert Path(command[2]).stat().st_mode & 0o777 == 0o700
            command = ["docker", *command[3:]]
        self.calls.append(command)
        if command[0] == "git":
            return self.source.encode()
        if command[1:3] == ["sts", "get-caller-identity"]:
            return json.dumps({"Account": self.account, "Arn":
                f"arn:aws:sts::{self.account}:assumed-role/deploy/session"}).encode()
        if command[1:3] == ["ecr", "describe-repositories"]:
            return json.dumps({"repositories": [{"repositoryUri": REFERENCE,
                                                 "imageTagMutability": self.mutability}]}).encode()
        if command[1:3] == ["ecr", "get-login-password"]:
            return b"synthetic-password"
        if command[1:3] == ["ecr", "batch-get-image"]:
            assert command[command.index("--repository-name") + 1] == REPOSITORY
            tag = command[command.index("--image-ids") + 1].split("=", 1)[1]
            if self.registry_fault:
                return json.dumps(self.registry_fault).encode()
            if tag not in self.registry:
                return json.dumps({"images": [], "failures": [{
                    "imageId": {"imageTag": tag}, "failureCode": "ImageNotFound"}]}).encode()
            return json.dumps({"images": [self.registry[tag]], "failures": []}).encode()
        if command[:2] == ["docker", "login"]:
            assert input == b"synthetic-password"
            return b""
        if command[:2] == ["docker", "build"]:
            context = Path(command[-1])
            contents = {file.name: file.read_bytes() for file in context.iterdir()}
            self.builds.append((command, contents))
            role = "provider" if "atx.zip" in contents else "check"
            labels = dict(command[i + 1].split("=", 1) for i, value in enumerate(command) if value == "--label")
            target = command[command.index("-t") + 1]
            self.local[target] = {"Id": "sha256:" + publisher.digest(target.encode()), "Os": "linux",
                                  "Architecture": "amd64", "Config": {"Labels": labels, "User": "10000:10000",
                                  "Entrypoint": ["python", "-m", f"l5_services.adapters.transform.{role}_runner"]}}
            manifest = json.dumps({"schemaVersion": 2, "config": {"digest": self.local[target]["Id"]},
                                   "layers": []})
            self.manifests[target] = manifest
            if self.containerd_store:
                self.local[target]["Id"] = "sha256:" + publisher.digest(manifest.encode())
            return b""
        if command[:3] == ["docker", "image", "inspect"]:
            return json.dumps([self.local[command[-1]]]).encode()
        if command[:2] == ["docker", "push"]:
            target = command[-1]
            tag = target.rsplit(":", 1)[1]
            assert tag not in self.registry, "Immutable tags must never be pushed twice"
            manifest = self.manifests[target]
            sha = "sha256:" + publisher.digest(manifest.encode())
            self.registry[tag] = {"registryId": "123456789012", "repositoryName": REPOSITORY,
                                  "imageId": {"imageTag": tag, "imageDigest": sha}, "imageManifest": manifest}
            self.local[REFERENCE + "@" + sha] = copy.deepcopy(self.local[target])
            if self.push_uncertain:
                self.push_uncertain = False
                raise ValueError("push acknowledgement lost")
            return b""
        if command[:2] == ["docker", "pull"]:
            assert command[-1] in self.local
            return b""
        raise AssertionError(command)


def publish(selected, runner, **kwargs):
    env, identity, artifacts, wheel, vendor = selected
    return publisher.publish(identity, artifacts, wheel, env, run=runner, download=lambda: vendor, **kwargs)


def test_builds_actual_pod_images_from_verified_minimal_contexts_and_one_repository(selected):
    runner = Runner()
    result = publish(selected, runner)
    assert result["phase"] == "artifacts"
    assert result["run_attempt"] == 1
    assert set(result["images"]) == {"provider", "checks"}
    assert all(image.startswith(REFERENCE + "@sha256:") for image in result["images"].values())
    assert not {"installed", "qualification", "live_accepted"} & result.keys()
    provider, checks = runner.builds
    assert set(provider[1]) == {"Dockerfile", "worker.whl", "atx.zip"}
    assert set(checks[1]) == {"Dockerfile", "worker.whl", "check-policy.json"}
    for label, (command, files) in zip(("provider", "checks"), runner.builds):
        assert files["Dockerfile"] == (ROOT / "modules/L5-services/executor-image/transform"
                                      / publisher.DOCKERFILES[label]).read_bytes()
        assert files["worker.whl"] == selected[3].read_bytes()
        assert "--provenance=false" in command and "linux/amd64" in command
        assert "modules/tools/validation" not in str(command)
    assert provider[1]["atx.zip"] == selected[4]
    assert json.loads(checks[1]["check-policy.json"]) == json.loads(
        selected[0]["ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON"])["check_policy"]
    assert not any(call[1] in {"lambda", "eks", "sqs"} for call in runner.calls if call[0] == "aws")


def test_retry_reuses_exact_immutable_images_instead_of_pushing_again(selected):
    runner = Runner()
    first = publish(selected, runner)
    selected[0]["GITHUB_RUN_ATTEMPT"] = "2"
    selected[1]["run_attempt"] = selected[2]["run_attempt"] = 2
    second = publish(selected, runner)
    assert first["images"] == second["images"]
    assert first["builds"] == second["builds"]
    assert second["run_attempt"] == 2
    assert len(runner.builds) == 2
    assert len([c for c in runner.calls if c[:2] == ["docker", "pull"]]) == 2


def test_lost_push_acknowledgement_is_reconciled_on_retry(selected):
    runner = Runner()
    runner.push_uncertain = True
    with pytest.raises(ValueError, match="acknowledgement"):
        publish(selected, runner)
    result = publish(selected, runner)
    assert len(runner.builds) == 2
    assert len(result["images"]) == 2


@pytest.mark.parametrize("fault", ["source", "account", "mutable", "attempt", "wheel", "vendor", "runtime"])
def test_changed_build_or_target_refused_before_docker(selected, fault):
    runner = Runner()
    env, identity, artifacts, wheel, vendor = selected
    if fault == "source":
        runner.source = "b" * 40
    elif fault == "account":
        runner.account = "999999999999"
    elif fault == "mutable":
        runner.mutability = "MUTABLE"
    elif fault == "attempt":
        artifacts["run_attempt"] = 2
    elif fault == "wheel":
        wheel.write_bytes(b"changed")
    elif fault == "vendor":
        selected = (*selected[:4], vendor + b"changed")
    else:
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("other.py", "# missing entrypoints")
        artifacts["artifacts"]["wheel"]["sha256"] = publisher.digest(wheel.read_bytes())
    with pytest.raises(ValueError):
        publish(selected, runner)
    assert not runner.builds
    if fault != "vendor":
        assert not any(c[0] == "docker" for c in runner.calls)


@pytest.mark.parametrize("fault", ["missing", "tag", "policy", "cwd", "timeout", "credential", "cycle"])
def test_invalid_protected_configuration_fails_before_external_calls(selected, fault):
    env = selected[0]
    config = json.loads(env["ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON"])
    if fault == "missing":
        env.pop("ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON")
    else:
        if fault == "tag":
            config["provider_base_image"] = "registry.example.invalid/base:latest"
        elif fault == "policy":
            config["check_policy"]["lock_sha256"] = "unverified"
        elif fault == "cycle":
            config["check_policy"]["image_digest"] = "sha256:" + "d" * 64
        else:
            command = config["check_policy"]["commands"][0]
            if fault == "cwd":
                command["cwd"] = "../outside"
            elif fault == "timeout":
                command["timeout_seconds"] = 121
            else:
                command["environment"] = {"AWS_ACCESS_KEY_ID": "synthetic"}
        env["ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON"] = json.dumps(config)
    runner = Runner()
    with pytest.raises(ValueError):
        publish(selected, runner)
    assert runner.calls == []


def test_registry_error_is_not_permission_to_build_or_replace(selected):
    runner = Runner()
    runner.registry_fault = {"images": [], "failures": [{"failureCode": "AccessDenied"}]}
    with pytest.raises(ValueError, match="absence was not established"):
        publish(selected, runner)
    assert runner.builds == []


@pytest.mark.parametrize("fault", ["manifest", "labels", "entrypoint", "user", "platform", "config"])
def test_existing_image_must_match_manifest_and_executor_contract(selected, fault):
    runner = Runner()
    result = publish(selected, runner)
    image = runner.local[result["images"]["provider"]]
    if fault == "manifest":
        next(iter(runner.registry.values()))["imageManifest"] += " "
    elif fault == "labels":
        image["Config"]["Labels"] = {}
    elif fault == "entrypoint":
        image["Config"]["Entrypoint"] = ["unrelated"]
    elif fault == "user":
        image["Config"]["User"] = "0"
    elif fault == "platform":
        image["Architecture"] = "arm64"
    else:
        image["Id"] = "sha256:" + "9" * 64
    with pytest.raises(ValueError):
        publish(selected, runner)
    assert len(runner.builds) == 2


def test_policy_change_builds_a_distinct_check_image_only(selected):
    runner = Runner()
    before = publish(selected, runner)
    config = json.loads(selected[0]["ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON"])
    config["check_policy"]["oracle_version"] = "pytest-v2"
    selected[0]["ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON"] = json.dumps(config)
    after = publish(selected, runner)
    assert before["images"]["provider"] == after["images"]["provider"]
    assert before["images"]["checks"] != after["images"]["checks"]
    assert len(runner.builds) == 3


def test_containerd_manifest_ids_are_verified_for_fresh_and_reused_images(selected):
    runner = Runner()
    runner.containerd_store = True
    first = publish(selected, runner)
    assert all(runner.local[image]["Id"] == image.rsplit("@", 1)[1] for image in first["images"].values())
    second = publish(selected, runner)
    assert first["images"] == second["images"]
    assert len(runner.builds) == 2


def test_changed_executable_is_rejected_even_with_matching_archive_hash(selected, monkeypatch):
    monkeypatch.setattr(publisher, "EXECUTABLE_SHA256", "0" * 64)
    runner = Runner()
    with pytest.raises(ValueError, match="executable"):
        publish(selected, runner)
    assert not runner.builds


def test_cached_images_do_not_depend_on_vendor_download_availability(selected):
    runner = Runner()
    first = publish(selected, runner)

    def unavailable():
        raise AssertionError("An existing verified image does not need a new vendor download")

    env, identity, artifacts, wheel, _ = selected
    second = publisher.publish(identity, artifacts, wheel, env, run=runner, download=unavailable)
    assert first["images"] == second["images"]
