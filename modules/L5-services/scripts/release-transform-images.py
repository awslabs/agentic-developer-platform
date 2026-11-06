"""Publish Transform Pod images; publication is not runtime qualification."""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.request import build_opener

from release_context import NoRedirect, release_identity, verify_principal
from transform_image_transport import ImageTransport


ROOT = Path(__file__).resolve().parents[3]
SHA256 = re.compile(r"[a-f0-9]{64}\Z")
IMAGE = re.compile(r"[a-z0-9][a-z0-9.:-]*/[a-z0-9][a-z0-9._/-]*@sha256:[a-f0-9]{64}\Z")
VENDOR_URL = "https://transform-cli.awsstatic.com/3.19.0/atx.zip"
VENDOR_SHA256 = "2538a09e24ab5a8b2abeef16fec824600d66b5b215dceccd11e5c4f4318e904e"
EXECUTABLE_SHA256 = "87117a33f1aab02e78e0ed216549edef49918329df55e756bebd84d8f79a10bf"
DOCKERFILES = {"provider": "Dockerfile", "checks": "Dockerfile.checks"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def execute(command, *, input=None, env=None, stdout=None):
    try:
        return subprocess.run(command, input=input, env=env,
                              stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=900, check=True).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Build/registry output may contain private configuration or credentials.
        raise ValueError("Transform image command failed: " + command[0]) from None


def aws_json(run, *arguments):
    return json.loads(run(["aws", *arguments, "--output", "json"]))


def build_configuration(environment):
    raw = environment.get("ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON", "")
    if not raw or len(raw) > 131072:
        raise ValueError("Set protected ADP_L5_TRANSFORM_IMAGE_CONFIG_JSON before Transform publication")
    try:
        config = json.loads(raw)
    except ValueError:
        raise ValueError("Transform image configuration must be JSON") from None
    if (not isinstance(config, dict) or set(config) != {
            "schema_version", "provider_base_image", "check_base_image", "check_policy"}
            or type(config["schema_version"]) is not int or config["schema_version"] != 1
            or any(not isinstance(config[key], str) or not IMAGE.fullmatch(config[key])
                   for key in ("provider_base_image", "check_base_image"))):
        raise ValueError("Transform requires version-1 configuration and two digest-pinned base images")
    policy = config["check_policy"]
    if (not isinstance(policy, dict) or set(policy) != {
            "bundle_sha256", "lock_sha256", "oracle_version", "commands"}
            or any(not isinstance(policy[key], str) or not SHA256.fullmatch(policy[key])
                   for key in ("bundle_sha256", "lock_sha256"))
            or not isinstance(policy["oracle_version"], str) or not policy["oracle_version"]
            or not isinstance(policy["commands"], list) or not 1 <= len(policy["commands"]) <= 32):
        raise ValueError("Transform check image needs the selected pinned test policy")
    for command in policy["commands"]:
        if (not isinstance(command, dict) or set(command) != {
                "argv", "cwd", "environment", "timeout_seconds", "minimum_test_count"}
                or not isinstance(command["argv"], list) or not command["argv"]
                or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in command["argv"])
                or not isinstance(command["cwd"], str) or not command["cwd"]
                or (command["cwd"] != "." and any(
                    part in {"", ".", ".."} for part in command["cwd"].split("/")))
                or "\\" in command["cwd"] or "\x00" in command["cwd"]
                or not isinstance(command["environment"], dict)
                or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                       or key.upper().startswith(("AWS_", "ADP_", "KUBERNETES_"))
                       or not isinstance(value, str) or "\x00" in value
                       for key, value in command["environment"].items())
                or type(command["timeout_seconds"]) is not int
                or not 1 <= command["timeout_seconds"] <= 120
                or type(command["minimum_test_count"]) is not int
                or command["minimum_test_count"] < 1):
            raise ValueError("Transform check command is invalid or exceeds the executor contract")
    return config


def download_vendor():
    with build_opener(NoRedirect()).open(VENDOR_URL, timeout=60) as response:
        if response.status != 200:
            raise ValueError("Transform vendor bundle is unavailable")
        data = response.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Transform vendor bundle exceeds its bound")
    return data


def verify_vendor(data):
    if digest(data) != VENDOR_SHA256:
        raise ValueError("Transform vendor archive differs from the reviewed bundle")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if digest(archive.read("atx")) != EXECUTABLE_SHA256:
            raise ValueError("Transform vendor executable differs from the reviewed bundle")


def registry_image(run, region, repository, account, tag):
    response = aws_json(run, "ecr", "batch-get-image", "--region", region,
                        "--repository-name", repository, "--image-ids", f"imageTag={tag}")
    images, failures = response.get("images", []), response.get("failures", [])
    if (not images and len(failures) == 1
            and failures[0].get("failureCode") == "ImageNotFound"
            and failures[0].get("imageId") == {"imageTag": tag}):
        return None
    if failures or len(images) != 1:
        raise ValueError("Transform registry readback failed; absence was not established")
    image = images[0]
    raw = image.get("imageManifest", "")
    identity = image.get("imageId", {})
    if (image.get("registryId") != account or image.get("repositoryName") != repository
            or identity.get("imageTag") != tag
            or identity.get("imageDigest") != "sha256:" + digest(raw.encode())):
        raise ValueError("Transform registry manifest identity differs from the selected image")
    manifest = json.loads(raw)
    config_digest = manifest.get("config", {}).get("digest", "")
    if (manifest.get("schemaVersion") != 2 or "manifests" in manifest
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", config_digest)):
        raise ValueError("Transform requires a single-platform image manifest")
    return {"digest": identity["imageDigest"], "config_digest": config_digest}


def inspect_image(run, reference, label, identity, expected_image=None, *, details=False):
    observed = json.loads(run(["docker", "image", "inspect", reference]))
    if len(observed) != 1:
        raise ValueError("Transform local image identity is missing")
    image = observed[0]
    config = image.get("Config", {})
    labels = config.get("Labels") or {}
    runner = "provider" if label == "provider" else "check"
    if (image.get("Os") != "linux" or image.get("Architecture") != "amd64"
            or config.get("User") != "10000:10000"
            or config.get("Entrypoint") != ["python", "-m", f"l5_services.adapters.transform.{runner}_runner"]
            or any(labels.get(key) != value for key, value in identity.items())
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image.get("Id", ""))
            # Classic Docker reports the config digest; its containerd image
            # store reports the manifest digest. Both must come from ECR's
            # verified manifest, never from a tag or a caller-supplied value.
            or (expected_image is not None and image["Id"] not in {
                expected_image["config_digest"], expected_image["digest"]})):
        raise ValueError("Transform image contents or build identity differ from the selected executor")
    return image if details else image["Id"]


def publish(context, artifacts, wheel, environment, *, run=execute, download=download_vendor):
    config = build_configuration(environment)
    ImageTransport.validate(environment)
    if (context != release_identity(environment) or context["service"] != "transform"
            or any(artifacts.get(key) != value for key, value in context.items())
            or any(not SHA256.fullmatch(artifacts.get("artifacts", {}).get(name, {}).get("sha256", ""))
                   for name in ("wheel", "archive"))):
        raise ValueError("Transform images require this exact artifact publication attempt")
    wheel_bytes = Path(wheel).read_bytes()
    if digest(wheel_bytes) != artifacts["artifacts"]["wheel"]["sha256"]:
        raise ValueError("Transform build wheel differs from the published wheel")
    with zipfile.ZipFile(io.BytesIO(wheel_bytes)) as package:
        if not {"l5_services/adapters/transform/provider_runner.py",
                "l5_services/adapters/transform/check_runner.py"} <= set(package.namelist()):
            raise ValueError("Transform wheel lacks the executor entry points")
    if run(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).decode().strip() != context["source_revision"]:
        raise ValueError("Transform Dockerfiles differ from the reviewed source checkout")
    region = environment.get("AWS_REGION", "")
    if not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]", region):
        raise ValueError("Transform publication needs the selected AWS region")
    principal = aws_json(run, "sts", "get-caller-identity")
    verify_principal(environment, principal)
    account = principal["Account"]
    repository = f"adp-{context['environment']}-l5-transform-executor"
    suffix = "amazonaws.com.cn" if region.startswith("cn-") else "amazonaws.com"
    registry = f"{account}.dkr.ecr.{region}.{suffix}"
    reference = f"{registry}/{repository}"
    observed = aws_json(run, "ecr", "describe-repositories", "--region", region,
                        "--repository-names", repository).get("repositories", [])
    if (len(observed) != 1 or observed[0].get("repositoryUri") != reference
            or observed[0].get("imageTagMutability") != "IMMUTABLE"):
        raise ValueError("Bootstrap the selected immutable Transform executor repository first")
    images, builds = {}, {}
    # Do not retain the registry token in the runner's shared Docker configuration.
    with tempfile.TemporaryDirectory(prefix="l5-transform-images-") as directory:
        staging = Path(directory)
        transport = ImageTransport(environment, staging, run, registry, region)
        local_run = transport.docker
        if transport.mode == "docker":
            transport.login_docker()
        for label, filename in DOCKERFILES.items():
            dockerfile = ROOT / "modules/L5-services/executor-image/transform" / filename
            base = config["provider_base_image" if label == "provider" else "check_base_image"]
            inputs = {"source_revision": context["source_revision"], "wheel_sha256": digest(wheel_bytes),
                      "dockerfile_sha256": digest(dockerfile.read_bytes()), "base_image": base,
                      "platform": "linux/amd64", "role": label}
            inputs.update({"vendor_sha256": VENDOR_SHA256, "executable_sha256": EXECUTABLE_SHA256}
                          if label == "provider" else {"policy_sha256": digest(canonical(config["check_policy"]))})
            build_digest = digest(canonical(inputs))
            tag = f"{label}-{build_digest}"
            labels = {"org.opencontainers.image.revision": context["source_revision"],
                      "com.adp.l5.transform.build-inputs-sha256": build_digest}
            existing = registry_image(run, region, repository, account, tag)
            if existing is not None:
                pinned = reference + "@" + existing["digest"]
                if transport.mode == "archive":
                    imported = transport.import_pinned(pinned, label + "-cached", existing["config_digest"])
                    inspect_image(local_run, imported, label, labels)
                else:
                    local_run(["docker", "pull", "--platform", "linux/amd64", pinned])
                    inspect_image(local_run, pinned, label, labels, existing)
            else:
                # Exact cached provider images need no vendor-network access.
                vendor = download() if label == "provider" else None
                if vendor is not None:
                    verify_vendor(vendor)
                build_base = transport.import_pinned(base, label + "-base") if transport.mode == "archive" else base
                build = staging / label
                build.mkdir(mode=0o700)
                (build / "worker.whl").write_bytes(wheel_bytes)
                shutil.copyfile(dockerfile, build / "Dockerfile")
                (build / ("atx.zip" if label == "provider" else "check-policy.json")).write_bytes(
                    vendor if label == "provider" else canonical(config["check_policy"]))
                target = reference + ":" + tag
                command = ["docker", "build", "--platform", "linux/amd64", "--provenance=false",
                           "--build-arg", f"BASE_IMAGE={build_base}", "-f", str(build / "Dockerfile"), "-t", target]
                if transport.mode == "archive":
                    command.append("--pull=false")
                for key, value in labels.items():
                    command.extend(["--label", key + "=" + value])
                local_run([*command, str(build)])
                inspected = inspect_image(local_run, target, label, labels, details=True)
                local_digest = inspected["Id"]
                config_digest = None
                if transport.mode == "archive":
                    archive, config_digest = transport.export(inspected, label)
                    transport.push_archive(archive, target)
                else:
                    local_run(["docker", "push", target])
                existing = registry_image(run, region, repository, account, tag)
                if existing is None or (existing["config_digest"] != config_digest if config_digest else
                                        local_digest not in {existing["config_digest"], existing["digest"]}):
                    raise ValueError("Transform pushed image differs from the local build")
                pinned = reference + "@" + existing["digest"]
            images[label] = pinned
            builds[label] = {"tag": tag, "inputs_sha256": build_digest, "inputs": inputs}
    return {**context, "wheel_sha256": artifacts["artifacts"]["wheel"]["sha256"],
            "archive_sha256": artifacts["artifacts"]["archive"]["sha256"],
            "images": images, "builds": builds}


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--check-config"]:
            build_configuration(os.environ)
        else:
            mode, context_path, artifacts_path, wheel_path, destination = sys.argv[1:]
            if mode != "--publish":
                raise ValueError("Unknown Transform publication mode")
            if Path(destination).exists():
                raise ValueError("Transform publication receipt already exists; inspect it before retrying")
            result = publish(json.loads(Path(context_path).read_bytes()),
                             json.loads(Path(artifacts_path).read_bytes()), wheel_path, os.environ)
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(result, output, sort_keys=True)
                output.write("\n")
    except ValueError as error:
        raise SystemExit(str(error) + "; no qualification was recorded") from None
    except Exception:
        raise SystemExit("Transform image publication failed; no qualification was recorded") from None
