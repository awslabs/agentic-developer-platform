import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish


BUILD = ROOT / "modules/connectors/devlake/scripts/build.sh"
SYFT_VERSION = "1.42.0"
GITLEAKS_VERSION = "8.30.0"
GO_PACKAGES = ("./plugins/github/api", "./plugins/jira/api", "./plugins/jira/tasks/apiv2models")


def run(*command, **kwargs):
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def scan_layers(layout, destination):
    index = json.loads((layout / "index.json").read_text())
    manifest = json.loads(publish.blob(layout, index["manifests"][0]))
    for number, descriptor in enumerate(manifest["layers"]):
        layer_root = destination / str(number)
        with tarfile.open(fileobj=io.BytesIO(publish.blob(layout, descriptor)), mode="r:*") as layer:
            for member in layer:
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("unsafe path in image layer")
                if member.isfile():
                    target = layer_root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("wb") as output:
                        shutil.copyfileobj(layer.extractfile(member), output)


def produce(args, finalizer_preflight):
    if (args.output.exists() or args.output.resolve().is_relative_to(ROOT) or
            args.output.with_name("sbom.json").exists()):
        raise ValueError("evidence must use new private paths outside the repository")
    publish.verify_target(args.repository_uri, args.account, args.region, "devlake-poc/" + args.component)
    if not publish.REVISION.fullmatch(args.revision) or not publish.DIGEST.fullmatch(args.expected_digest):
        raise ValueError("full customization revision and image digest are required")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if actual != args.revision:
        raise ValueError("evidence revision differs from checked out source")
    run(str(BUILD), "--customization", args.revision, "--check")
    schema_digest = publish.inspect(args.layout, args.expected_digest, args.revision, args.component)

    with tempfile.TemporaryDirectory(prefix="devlake-checks-") as temporary:
        source = Path(temporary) / "upstream"
        run(str(BUILD), "--customization", args.revision, "--destination", str(source))
        run("go", "test", "-mod=readonly", *GO_PACKAGES, cwd=source / "backend",
            env={**os.environ, "CGO_ENABLED": "0", "GOTOOLCHAIN": "local"})

        syft_version = json.loads(subprocess.check_output(["syft", "version", "-o", "json"], text=True))
        if syft_version["version"] != SYFT_VERSION:
            raise ValueError("SBOM scanner version differs from checked toolchain")
        sbom = json.loads(subprocess.check_output(["syft", "scan", "oci-dir:" + str(args.layout),
                                                  "-o", "cyclonedx-json"], text=True, stderr=subprocess.DEVNULL))
        if sbom.get("bomFormat") != "CycloneDX" or not sbom.get("components"):
            raise ValueError("image SBOM is empty or malformed")

        gitleaks_version = subprocess.check_output(["gitleaks", "version"], text=True).strip().lstrip("v")
        if gitleaks_version != GITLEAKS_VERSION:
            raise ValueError("secret scanner version differs from checked toolchain")
        layers = Path(temporary) / "layers"
        scan_layers(args.layout, layers)
        run("gitleaks", "dir", "--no-banner", "--redact", str(layers))
        finalizer_preflight(args.repository_uri, args.expected_digest, args.component)

    sbom_bytes = (json.dumps(sbom, sort_keys=True) + "\n").encode()
    evidence = {
        "revision": args.revision,
        "component": args.component,
        "image_digest": args.expected_digest,
        "schema_sha256": schema_digest,
        "sbom_sha256": hashlib.sha256(sbom_bytes).hexdigest(),
        "checks": {check: "passed" for check in publish.REQUIRED_CHECKS},
    }
    return sbom_bytes, evidence


def main():
    parser = argparse.ArgumentParser(description="Verify pinned DevLake image before optional publication")
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--component", choices=("api", "worker"), required=True)
    parser.add_argument("--expected-digest", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--repository-uri", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    from finalize import preflight
    sbom, evidence = produce(args, preflight)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(args.output.with_name("sbom.json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
        output.write(sbom)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(evidence, output, sort_keys=True)
        output.write("\n")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError, json.JSONDecodeError, ImportError):
        print("DevLake checks refused; no publication evidence produced", file=sys.stderr)
        sys.exit(1)
