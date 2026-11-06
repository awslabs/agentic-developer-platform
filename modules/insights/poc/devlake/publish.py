import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
SOURCE_LOCK = ROOT / "modules/connectors/devlake/source-lock.json"
SCHEMA = ROOT / "modules/insights/poc/devlake/schema-manifest.json"
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*\Z")
REQUIRED_CHECKS = frozenset({"clean-build", "source-lock", "go-unit", "oci-content",
                             "license", "sbom", "secrets", "finalizer"})


def blob(layout, descriptor):
    digest = descriptor["digest"]
    if not DIGEST.fullmatch(digest):
        raise ValueError("invalid image blob digest")
    content = (layout / "blobs" / "sha256" / digest[7:]).read_bytes()
    if hashlib.sha256(content).hexdigest() != digest[7:] or len(content) != descriptor["size"]:
        raise ValueError("image blob does not match digest")
    return content


def inspect(layout, expected_digest, revision, component):
    if component not in ("api", "worker") or not DIGEST.fullmatch(expected_digest) or not REVISION.fullmatch(revision):
        raise ValueError("immutable image digest and customization commit are required")
    index = json.loads((layout / "index.json").read_text())
    if len(index["manifests"]) != 1 or index["manifests"][0]["digest"] != expected_digest:
        raise ValueError("image digest differs from selected layout")
    manifest = json.loads(blob(layout, index["manifests"][0]))
    config = json.loads(blob(layout, manifest["config"]))
    lock = json.loads(SOURCE_LOCK.read_text())
    operating_system, architecture = lock["platform"].split("/")
    executable = "connector-api" if component == "api" else "collector-worker"
    if (config["os"] != operating_system or config["architecture"] != architecture or
            config["config"]["Entrypoint"] != ["/usr/local/bin/" + executable] or
            config["config"]["Labels"]["org.opencontainers.image.revision"] != revision):
        raise ValueError("image configuration differs from approved component")
    if len(manifest["layers"]) < 2:
        raise ValueError("runtime base and component layers are required")
    for descriptor in manifest["layers"]:
        blob(layout, descriptor)
    with tarfile.open(layout / "blobs" / "sha256" / manifest["layers"][-1]["digest"][7:], "r:*") as layer:
        members = set(layer.getnames())
        expected = {"usr/local/bin/" + executable, "usr/share/licenses/devlake/LICENSE",
                    "usr/share/licenses/devlake/NOTICE", "usr/share/devlake/schema-manifest.json",
                    "usr/share/devlake/source-manifest.json", "usr/share/devlake/module-contract.json"}
        if members != expected:
            raise ValueError("component layer has unexpected contents")
        schema = layer.extractfile("usr/share/devlake/schema-manifest.json").read()
        source = json.load(layer.extractfile("usr/share/devlake/source-manifest.json"))
        contract_bytes = layer.extractfile("usr/share/devlake/module-contract.json").read()
        contract = json.loads(contract_bytes)
        license_bytes = layer.extractfile("usr/share/licenses/devlake/LICENSE").read()
        notice_bytes = layer.extractfile("usr/share/licenses/devlake/NOTICE").read()
        entry = (ROOT / "modules/connectors/devlake/cmd/connector-api/main.go" if component == "api" else
                 ROOT / "modules/insights/poc/devlake/cmd/collector-worker/main.go")
        if (schema != SCHEMA.read_bytes() or source["schema_sha256"] != hashlib.sha256(schema).hexdigest() or
                source["entry_sha256"] != hashlib.sha256(entry.read_bytes()).hexdigest() or
                source["component"] != component or source["customization_commit"] != revision or
                source["upstream"] != lock["upstream"] or source["patches"] != lock["patches"] or
                source["oci_packager"] != lock["oci_packager"] or source["images"] != lock["images"] or
                source["platform"] != lock["platform"] or source["toolchain"] != lock["toolchain"] or
                contract["default_enabled"] is not False or
                source["module_contract_sha256"] != hashlib.sha256(contract_bytes).hexdigest() or
                source["license_sha256"] != hashlib.sha256(license_bytes).hexdigest() or
                source["notice_sha256"] != hashlib.sha256(notice_bytes).hexdigest()):
            raise ValueError("image source, schema or module contract differs from checked revision")
    return hashlib.sha256(schema).hexdigest()


def check_evidence(checks_path, revision, component, digest, schema_digest):
    evidence = json.loads(checks_path.read_text())
    if (evidence["revision"] != revision or evidence["component"] != component or
            evidence["image_digest"] != digest or evidence["schema_sha256"] != schema_digest or
            any(evidence["checks"].get(name) != "passed" for name in REQUIRED_CHECKS)):
        raise ValueError("required pre-publication checks are missing or failed")
    sbom_digest = evidence.get("sbom_sha256")
    if (not isinstance(sbom_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", sbom_digest) or
            hashlib.sha256(checks_path.with_name("sbom.json").read_bytes()).hexdigest() != sbom_digest):
        raise ValueError("image SBOM differs from checked publication evidence")
    return sbom_digest


def verify_target(uri, account, region, repository):
    if (not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]", region) or
            not REPOSITORY.fullmatch(repository) or
            uri != f"{account}.dkr.ecr.{region}.amazonaws.com/{repository}"):
        raise ValueError("publication destination is not the exact scoped ECR repository")


def broker(label, *command):
    try:
        return subprocess.check_output(["adp-cred", "assume", "--service", "aws", "--label", label,
                                        "--exec", *command], stderr=subprocess.DEVNULL, text=True).strip()
    except subprocess.CalledProcessError as error:
        raise ValueError("selected credential broker operation failed") from None

def existing_digest(args, tag):
    result = json.loads(broker(args.connection_label, "aws", "ecr", "batch-get-image", "--region",
                               args.region, "--repository-name", args.repository, "--image-ids",
                               "imageTag=" + tag, "--output", "json"))
    images = result["images"]
    failures = result["failures"]
    if failures:
        if (images or len(failures) != 1 or failures[0]["failureCode"] != "ImageNotFound" or
                failures[0]["imageId"] != {"imageTag": tag}):
            raise ValueError("ECR tag lookup failed; publication refused")
        return None
    if len(images) != 1:
        raise ValueError("ECR tag lookup returned an ambiguous image")
    image = images[0]
    image_id = image["imageId"]
    if (image["registryId"] != args.account or image["repositoryName"] != args.repository or
            image_id["imageTag"] != tag or not DIGEST.fullmatch(image_id["imageDigest"])):
        raise ValueError("ECR tag lookup differs from publication scope")
    return image_id["imageDigest"]


def publish(args, finalizer_preflight):
    if (os.environ.get("GITHUB_REPOSITORY") != "aws-e/adp" or
            os.environ.get("GITHUB_REF") != "refs/heads/main" or
            os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise ValueError("only protected manual main-branch publication is supported")
    verify_target(args.repository_uri, args.account, args.region, args.repository)
    if not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", args.role):
        raise ValueError("publisher role name is invalid")
    current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if args.revision != current:
        raise ValueError("publication revision differs from checked out source")
    schema_digest = inspect(args.layout, args.expected_digest, args.revision, args.component)
    sbom_digest = check_evidence(args.checks, args.revision, args.component, args.expected_digest, schema_digest)
    identity = json.loads(broker(args.connection_label, "aws", "sts", "get-caller-identity", "--output", "json"))
    expected_arn = f"arn:aws:sts::{args.account}:assumed-role/{args.role}/"
    if identity["Account"] != args.account or not identity["Arn"].startswith(expected_arn):
        raise ValueError("selected AWS account or exact assumed role differs from publication scope")
    finalizer_preflight(args.repository_uri, args.expected_digest, args.component)
    repository = json.loads(broker(args.connection_label, "aws", "ecr", "describe-repositories", "--region",
                                   args.region, "--repository-names", args.repository, "--output", "json"))["repositories"]
    if (len(repository) != 1 or repository[0]["repositoryUri"] != args.repository_uri or
            repository[0]["registryId"] != args.account or repository[0]["imageTagMutability"] != "IMMUTABLE"):
        raise ValueError("ECR repository, account or immutability policy differs from publication scope")
    tag = f"{args.revision}-{args.component}"
    observed = existing_digest(args, tag)
    if observed is not None and observed != args.expected_digest:
        raise ValueError("immutable ECR tag belongs to a different digest; no publication receipt")
    with tempfile.TemporaryDirectory(prefix="devlake-publisher-") as temporary:
        environment = {**os.environ, "DOCKER_CONFIG": temporary}
        password = broker(args.connection_label, "aws", "ecr", "get-login-password", "--region", args.region)
        try:
            subprocess.run(["crane", "auth", "login", args.repository_uri.split("/")[0], "-u", "AWS",
                            "--password-stdin"], input=password, env=environment, capture_output=True,
                           text=True, check=True)
            if observed is None:
                subprocess.run(["crane", "push", str(args.layout), args.repository_uri + ":" + tag],
                               env=environment, capture_output=True, text=True, check=True)
            readback = subprocess.check_output(["crane", "digest", args.repository_uri + ":" + tag],
                                               env=environment, stderr=subprocess.DEVNULL, text=True).strip()
        except subprocess.CalledProcessError:
            raise ValueError("registry push or digest readback failed; no publication receipt") from None
    if readback != args.expected_digest:
        raise ValueError("registry image digest differs from checked artifact; no publication receipt")
    registry_image = json.loads(broker(args.connection_label, "aws", "ecr", "describe-images", "--region",
                                       args.region, "--repository-name", args.repository, "--image-ids",
                                       "imageTag=" + tag, "--output", "json"))["imageDetails"]
    if len(registry_image) != 1 or registry_image[0]["imageDigest"] != readback:
        raise ValueError("ECR digest readback differs from checked artifact; no publication receipt")
    finalizer_preflight(args.repository_uri, args.expected_digest, args.component)
    return {"component": args.component, "revision": args.revision, "image_digest": readback,
            "repository_uri": args.repository_uri, "schema_sha256": schema_digest, "sbom_sha256": sbom_digest}


def main():
    parser = argparse.ArgumentParser(description="Publish verified DevLake image with scoped authority")
    parser.add_argument("--publish", action="store_true", required=True)
    parser.add_argument("--component", choices=("api", "worker"), required=True)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--checks", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repository-uri", required=True)
    parser.add_argument("--connection-label", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if args.receipt.exists() or args.receipt.resolve().is_relative_to(ROOT):
        parser.error("receipt must be a new path outside the public repository")
    from finalize import preflight
    receipt = publish(args, preflight)
    temporary = args.receipt.with_suffix(".pending")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(receipt, output, sort_keys=True)
        output.write("\n")
    temporary.rename(args.receipt)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, json.JSONDecodeError) as error:
        print(f"DevLake publication refused: {error}", file=sys.stderr)
        sys.exit(1)
