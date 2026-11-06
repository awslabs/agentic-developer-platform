"""Publish immutable validation images; verify installation only in a separate live readback."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from release_context import release_identity, verify_principal


REPOSITORY = re.compile(r"([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/([a-z0-9_.\-/]+)\Z")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}\Z")
IMAGES = {
    "service": ("ADP_VALIDATION_SERVICE_REPOSITORY", "modules/tools/validation/Dockerfile"),
    "checks": ("ADP_VALIDATION_CHECK_REPOSITORY", "modules/tools/validation/isolation-infra/checks.Dockerfile"),
}


def execute(command, *, input=None):
    try:
        return subprocess.run(command, input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=900, check=True).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError("Validation image publication or readback failed") from None


def aws_json(run, *arguments):
    return json.loads(run(["aws", *arguments, "--output", "json"]))


def selected_repositories(environment, account):
    selected = {}
    for label, (key, dockerfile) in IMAGES.items():
        reference = environment.get(key, "")
        match = REPOSITORY.fullmatch(reference)
        if (match is None or match[1] != account or match[2] != environment.get("AWS_REGION")):
            raise ValueError("Validation repository differs from the selected AWS target")
        selected[label] = (reference, match[3], dockerfile)
    if selected["service"][0] == selected["checks"][0]:
        raise ValueError("Validation service and checks need distinct immutable repositories")
    return selected


def publish(context, artifacts, environment, *, run=execute):
    if (context != release_identity(environment) or context["service"] != "transform"
            or any(artifacts.get(key) != context[key] for key in ("source_revision", "service", "run_id"))
            or not all(artifacts.get("artifacts", {}).get(name, {}).get("sha256") for name in ("wheel", "archive"))):
        raise ValueError("Validation images require the selected Transform artifact attempt")
    principal = aws_json(run, "sts", "get-caller-identity")
    verify_principal(environment, principal)
    selected = selected_repositories(environment, principal["Account"])
    for reference, repository, _ in selected.values():
        response = aws_json(run, "ecr", "describe-repositories", "--region", environment["AWS_REGION"],
                            "--repository-names", repository)
        repositories = response.get("repositories", [])
        if (len(repositories) != 1 or repositories[0].get("repositoryUri") != reference
                or repositories[0].get("imageTagMutability") != "IMMUTABLE"):
            raise ValueError("Validation image repository must exist with immutable tags")
    password = run(["aws", "ecr", "get-login-password", "--region", environment["AWS_REGION"]])
    if not password.strip():
        raise ValueError("Validation image registry login unavailable")
    run(["docker", "login", "--username", "AWS", "--password-stdin",
         selected["service"][0].split("/", 1)[0]], input=password)
    images = {}
    for label, (reference, repository, dockerfile) in selected.items():
        tag = f"source-{context['source_revision']}"
        run(["docker", "build", "--platform", "linux/amd64", "--provenance=false",
             "--label", f"org.opencontainers.image.revision={context['source_revision']}",
             "-f", dockerfile, "-t", f"{reference}:{tag}", "."])
        run(["docker", "push", f"{reference}:{tag}"])
        response = aws_json(run, "ecr", "describe-images", "--region", environment["AWS_REGION"],
                            "--repository-name", repository, "--image-ids", f"imageTag={tag}")
        details = response.get("imageDetails", [])
        if len(details) != 1 or not DIGEST.fullmatch(details[0].get("imageDigest", "")):
            raise ValueError("Validation image digest was not confirmed by ECR")
        images[label] = f"{reference}@{details[0]['imageDigest']}"
    return {"schema_version": 1, "phase": "artifacts", "service": "transform",
            "source_revision": context["source_revision"], "run_id": context["run_id"],
            "wheel_sha256": artifacts["artifacts"]["wheel"]["sha256"],
            "archive_sha256": artifacts["artifacts"]["archive"]["sha256"], "images": images}


def installed(manifest, environment, *, run=execute):
    if manifest.get("phase") != "artifacts" or manifest.get("service") != "transform":
        raise ValueError("Only pinned Transform images can be checked")
    images = manifest.get("images", {})
    source = manifest.get("source_revision", "")
    if not re.fullmatch(r"[a-f0-9]{40}", source):
        raise ValueError("Installed readback needs the reviewed source revision")
    principal = aws_json(run, "sts", "get-caller-identity")
    verify_principal(environment, principal)
    selected = selected_repositories(environment, principal["Account"])
    for label, (reference, repository, _) in selected.items():
        image = images.get(label)
        if (not isinstance(image, str) or not image.startswith(reference + "@")
                or not DIGEST.fullmatch(image[len(reference) + 1:])):
            raise ValueError("Installed image differs from the selected repository")
        response = aws_json(run, "ecr", "describe-images", "--region", environment["AWS_REGION"],
                            "--repository-name", repository, "--image-ids", f"imageTag=source-{source}")
        details = response.get("imageDetails", [])
        if len(details) != 1 or images[label] != f"{reference}@{details[0].get('imageDigest')}":
            raise ValueError("Selected registry image digest changed")
    function = environment["ADP_VALIDATION_FUNCTION_NAME"]
    namespace = environment["ADP_VALIDATION_NAMESPACE"]
    pod_name = environment["ADP_VALIDATION_CHECK_POD"]
    response = aws_json(run, "lambda", "get-function", "--function-name", function)
    if response.get("Code", {}).get("ResolvedImageUri") != images["service"]:
        raise ValueError("Installed validation service differs from published image")
    pod = json.loads(run(["kubectl", "get", "pod", pod_name, "-n", namespace, "-o", "json"]))
    metadata = pod.get("metadata", {})
    containers = pod.get("spec", {}).get("containers", [])
    statuses = pod.get("status", {}).get("containerStatuses", [])
    if (metadata.get("name") != pod_name or metadata.get("namespace") != namespace
            or not metadata.get("uid") or len(containers) != 1 or len(statuses) != 1
            or containers[0].get("image") != images["checks"]
            or not statuses[0].get("imageID", "").endswith("@" + images["checks"].rsplit("@", 1)[1])):
        raise ValueError("Observed validation check Pod differs from published image")
    return {"phase": "installed-readback", "source_revision": manifest["source_revision"],
            "images": images, "check_pod_uid": metadata["uid"]}


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["--publish"]:
            _, context_path, artifacts_path, destination = sys.argv[1:]
            result = publish(json.loads(Path(context_path).read_bytes()),
                             json.loads(Path(artifacts_path).read_bytes()), os.environ)
        elif sys.argv[1:2] == ["--installed"]:
            _, artifacts_path, destination = sys.argv[1:]
            result = installed(json.loads(Path(artifacts_path).read_bytes()), os.environ)
        else:
            raise ValueError("Unknown validation image release mode")
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(result, output, sort_keys=True)
            output.write("\n")
    except Exception:
        raise SystemExit("Validation image release or installed readback failed; no qualification was recorded") from None
