import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REVISION = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"[a-z0-9./_-]+:[a-z0-9._-]+@sha256:[0-9a-f]{64}\Z")


def read_lock(path):
    lock = json.loads(path.read_text())
    upstream = lock["upstream"]
    if lock["schema_version"] != 1 or upstream["url"] != "https://github.com/apache/devlake.git":
        raise ValueError("unsupported source lock")
    if not REVISION.fullmatch(upstream["revision"]):
        raise ValueError("upstream revision must be a full commit SHA")
    if any(not DIGEST.fullmatch(upstream[field]) for field in ("go_mod_sha256", "go_sum_sha256")):
        raise ValueError("dependency lock digest must be SHA-256")
    if lock["toolchain"] != "go1.26.0" or lock["platform"] not in ("linux/amd64", "linux/arm64"):
        raise ValueError("unsupported toolchain or platform")
    if any(not IMAGE.fullmatch(lock["images"][name]) for name in ("builder", "runtime")):
        raise ValueError("base images must carry immutable digests")
    packager = lock["oci_packager"]
    if packager["module"] != "github.com/google/go-containerregistry" or packager["version"] != "v0.20.7":
        raise ValueError("unsupported OCI packager")
    for filename, field in (("go.mod", "go_mod_sha256"), ("go.sum", "go_sum_sha256")):
        actual = hashlib.sha256((ROOT / "oci" / filename).read_bytes()).hexdigest()
        if not DIGEST.fullmatch(packager[field]) or actual != packager[field]:
            raise ValueError(f"OCI packager dependency lock mismatch: {filename}")
    patches = lock["patches"]
    if not patches or any(Path(patch["path"]).name != patch["path"] for patch in patches):
        raise ValueError("missing or unsafe patch inventory")
    series = (ROOT / "patches" / "series").read_text().splitlines()
    if series != [patch["path"] for patch in patches]:
        raise ValueError("patch series differs from source lock")
    for patch in patches:
        if not DIGEST.fullmatch(patch["sha256"]):
            raise ValueError("patch digest must be SHA-256")
        actual = hashlib.sha256((ROOT / "patches" / patch["path"]).read_bytes()).hexdigest()
        if actual != patch["sha256"]:
            raise ValueError("patch digest mismatch")
    return lock


def run(*command, cwd=None, env=None):
    subprocess.run(command, cwd=cwd, env=env, check=True)


def checkout(lock, destination):
    upstream = lock["upstream"]
    run("git", "init", "-q", str(destination))
    run("git", "-C", str(destination), "remote", "add", "origin", upstream["url"])
    run("git", "-C", str(destination), "fetch", "-q", "--depth=1", "origin", upstream["revision"])
    run("git", "-C", str(destination), "checkout", "-q", "--detach", "FETCH_HEAD")
    actual = subprocess.check_output(["git", "-C", str(destination), "rev-parse", "HEAD"], text=True).strip()
    if actual != upstream["revision"]:
        raise ValueError("fetched revision does not match source lock")
    for path, digest_field in (("go.mod", "go_mod_sha256"), ("go.sum", "go_sum_sha256")):
        actual_digest = hashlib.sha256((destination / "backend" / path).read_bytes()).hexdigest()
        if actual_digest != upstream[digest_field]:
            raise ValueError(f"dependency lock mismatch: {path}")


def stage(lock, destination, customization, component):
    worker = ROOT.parents[1] / "insights" / "poc" / "devlake"
    entry = (worker / "cmd/collector-worker/main.go" if component == "worker"
             else ROOT / "cmd/connector-api/main.go")
    target = destination / "backend" / "cmd" / ("adp-collector-worker" if component == "worker" else "adp-connector-api")
    target.mkdir(parents=True, exist_ok=True)
    for source in entry.parent.glob("*.go"):
        if not source.name.endswith("_test.go"):
            shutil.copyfile(source, target / source.name)
    if component == "api":
        shutil.copytree(ROOT / "providers", destination / "backend/adp/providers",
                        ignore=shutil.ignore_patterns("*_test.go", "README.md"), dirs_exist_ok=True)
    if component == "worker":
        shutil.copytree(worker / "cmd/collector-worker/internal", target / "internal", dirs_exist_ok=True)
        shutil.copytree(ROOT / "sourceapi", target / "internal/sourceapi", dirs_exist_ok=True)
        shutil.copytree(ROOT / "normalization", destination / "backend/adp/normalization",
                        ignore=shutil.ignore_patterns("*_test.go", "README.md", "testdata"), dirs_exist_ok=True)
    assets = destination / "backend" / "adp"
    assets.mkdir(exist_ok=True)
    schema = worker / "schema-manifest.json"
    shutil.copyfile(schema, assets / "schema-manifest.json")
    contract = (worker if component == "worker" else ROOT) / "module-contract.json"
    shutil.copyfile(contract, assets / "module-contract.json")
    for filename in ("LICENSE", "NOTICE"):
        shutil.copyfile(destination / filename, assets / filename)
    manifest = {
        "schema_version": 1,
        "component": component,
        "upstream": lock["upstream"],
        "customization_commit": customization,
        "platform": lock["platform"],
        "toolchain": lock["toolchain"],
        "images": lock["images"],
        "oci_packager": lock["oci_packager"],
        "patches": lock["patches"],
        "schema_sha256": hashlib.sha256(schema.read_bytes()).hexdigest(),
        "module_contract_sha256": hashlib.sha256(contract.read_bytes()).hexdigest(),
        "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
        "license_sha256": hashlib.sha256((destination / "LICENSE").read_bytes()).hexdigest(),
        "notice_sha256": hashlib.sha256((destination / "NOTICE").read_bytes()).hexdigest(),
    }
    (assets / "source-manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    return manifest


def write_layer(binary, assets, layer_path, component):
    executable = "connector-api" if component == "api" else "collector-worker"
    entries = [(binary, f"usr/local/bin/{executable}", 0o755)]
    for name in ("LICENSE", "NOTICE"):
        entries.append((assets / name, f"usr/share/licenses/devlake/{name}", 0o644))
    for name in ("source-manifest.json", "schema-manifest.json", "module-contract.json"):
        entries.append((assets / name, f"usr/share/devlake/{name}", 0o644))
    with tarfile.open(layer_path, "w") as archive:
        for path, name, mode in entries:
            metadata = archive.gettarinfo(str(path), arcname=name)
            metadata.uid = 0
            metadata.gid = 0
            metadata.uname = ""
            metadata.gname = ""
            metadata.mtime = 0
            metadata.mode = mode
            with path.open("rb") as content:
                archive.addfile(metadata, content)


def validate_oci(layout, lock, destination, component, customization, binary):
    index = json.loads((layout / "index.json").read_text())
    if len(index["manifests"]) != 1:
        raise ValueError("OCI layout must contain exactly one image")

    def blob(descriptor):
        digest = descriptor["digest"]
        if not digest.startswith("sha256:") or not DIGEST.fullmatch(digest[7:]):
            raise ValueError("invalid OCI blob digest")
        content = (layout / "blobs" / "sha256" / digest[7:]).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest[7:] or len(content) != descriptor["size"]:
            raise ValueError("missing or corrupted OCI blob")
        return content

    manifest = json.loads(blob(index["manifests"][0]))
    if len(manifest["layers"]) < 2:
        raise ValueError("base and component layers are required")
    for layer in manifest["layers"]:
        blob(layer)
    config = json.loads(blob(manifest["config"]))
    operating_system, architecture = lock["platform"].split("/")
    executable = "connector-api" if component == "api" else "collector-worker"
    if (config["os"] != operating_system or config["architecture"] != architecture or
            config["config"]["Entrypoint"] != [f"/usr/local/bin/{executable}"] or
            config["config"]["User"] != "65532:65532" or
            config["config"]["Labels"]["org.opencontainers.image.revision"] != customization):
        raise ValueError("OCI configuration does not match selected source and architecture")
    layer_path = layout / "blobs" / "sha256" / manifest["layers"][-1]["digest"][7:]
    with tarfile.open(layer_path, "r:*") as archive:
        expected = {f"usr/local/bin/{executable}", "usr/share/licenses/devlake/LICENSE",
                    "usr/share/licenses/devlake/NOTICE", "usr/share/devlake/source-manifest.json",
                    "usr/share/devlake/schema-manifest.json", "usr/share/devlake/module-contract.json"}
        if set(archive.getnames()) != expected:
            raise ValueError("unexpected OCI component layer contents")
        for name in expected:
            staged = (binary if name.endswith("/" + executable) else
                      destination / "backend" / "adp" / name.rsplit("/", 1)[-1])
            payload = archive.extractfile(name).read()
            if hashlib.sha256(payload).digest() != hashlib.sha256(staged.read_bytes()).digest():
                raise ValueError("OCI layer differs from staged component")
    return index["manifests"][0]["digest"]


def build_oci(lock, destination, customization, component, output, temporary):
    if output.exists() or not output.parent.is_dir():
        raise ValueError("OCI output must be a new directory with an existing parent")
    version = subprocess.check_output(["go", "version"], text=True)
    if not re.search(r"\b" + re.escape(lock["toolchain"]) + r"\s", version):
        raise ValueError("local Go toolchain differs from source lock")
    binary = temporary / ("connector-api" if component == "api" else "collector-worker")
    operating_system, architecture = lock["platform"].split("/")
    environment = {**os.environ, "GOOS": operating_system, "GOARCH": architecture,
                   "CGO_ENABLED": "0", "GOTOOLCHAIN": "local"}
    run("go", "build", "-mod=readonly", "-trimpath", "-ldflags=-buildid=", "-o", str(binary),
        "./cmd/adp-" + ("connector-api" if component == "api" else "collector-worker"),
        cwd=destination / "backend", env=environment)
    layer_path = temporary / "component.tar"
    write_layer(binary, destination / "backend" / "adp", layer_path, component)
    layout = temporary / "image"
    run("go", "run", "-mod=readonly", ".", "--base", lock["images"]["runtime"],
        "--platform", lock["platform"], "--layer", str(layer_path), "--output", str(layout),
        "--revision", customization, "--component", component, cwd=ROOT / "oci", env=environment)
    validate_oci(layout, lock, destination, component, customization, binary)
    layout.rename(output)


def main():
    parser = argparse.ArgumentParser(description="Prepare pinned DevLake source and build isolated component images")
    parser.add_argument("--lock", type=Path, default=ROOT / "source-lock.json")
    parser.add_argument("--customization", required=True, help="full ADP commit containing the build recipe")
    parser.add_argument("--check", action="store_true", help="validate locks without fetching")
    parser.add_argument("--destination", type=Path, help="new directory to receive pinned source")
    parser.add_argument("--component", choices=("api", "worker"), help="component to build")
    parser.add_argument("--tag", help="local, unpublished Docker image tag")
    parser.add_argument("--engine", choices=("oci", "docker"), default="oci")
    parser.add_argument("--output", type=Path, help="new local OCI image-layout directory")
    args = parser.parse_args()
    lock = read_lock(args.lock)
    if not REVISION.fullmatch(args.customization):
        raise ValueError("customization must be a full commit SHA")
    actual_customization = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if actual_customization != args.customization:
        raise ValueError("customization commit does not match checked out ADP source")
    if args.check:
        return
    if args.component is None and args.destination is None:
        raise ValueError("choose a component or a new destination for source inspection")
    if args.component is None and (args.tag is not None or args.output is not None):
        raise ValueError("a tag or output requires a selected component")
    if args.component is not None:
        if args.engine == "docker":
            expected = f"localhost/devlake-{args.component}:{args.customization}"
            if args.tag != expected:
                raise ValueError(f"image tag must be {expected}")
        elif args.output is None or args.tag is not None:
            raise ValueError("OCI image builds require --output and refuse Docker tags")
        changed = subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=all", "--",
             "modules/connectors/devlake", "modules/insights/poc/devlake"],
            cwd=ROOT.parents[2], text=True,
        )
        if changed:
            raise ValueError("uncommitted component sources cannot be represented by customization commit")
    with tempfile.TemporaryDirectory(prefix="devlake-build-") as temporary:
        destination = args.destination or Path(temporary) / "upstream"
        if destination.exists():
            raise ValueError("a new destination directory is required")
        checkout(lock, destination)
        for patch in lock["patches"]:
            run("git", "-C", str(destination), "apply", "--unidiff-zero", "--check", str(ROOT / "patches" / patch["path"]))
            run("git", "-C", str(destination), "apply", "--unidiff-zero", str(ROOT / "patches" / patch["path"]))
        if args.component is None:
            return
        stage(lock, destination, args.customization, args.component)
        if args.engine == "oci":
            build_oci(lock, destination, args.customization, args.component, args.output, Path(temporary))
        else:
            dockerfile = (ROOT.parents[1] / "insights" / "poc" / "devlake" / "Dockerfile"
                          if args.component == "worker" else ROOT / "Dockerfile")
            run("docker", "build", "--pull", "--platform", lock["platform"],
                "--build-arg", "BUILDER_IMAGE=" + lock["images"]["builder"],
                "--build-arg", "RUNTIME_IMAGE=" + lock["images"]["runtime"],
                "--build-arg", "SOURCE_REVISION=" + args.customization,
                "-f", str(dockerfile), "-t", args.tag, str(destination / "backend"))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"DevLake source rejected: {error}", file=sys.stderr)
        sys.exit(1)
