"""Prepare a reviewed native image; never discover versions or repair enrollment."""

import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile

APP_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_ROOT / "executor"))
from superplane_executor import node_bootstrap_runner as bootstrap  # noqa: E402
from superplane_executor import node_runner as runner  # noqa: E402

SOURCE_FILES = (
    "executor/node-command/node-bootstrap-v1",
    "executor/node-command/node-probe-v1",
    "executor/superplane_executor/node_runner.py",
    "executor/superplane_executor/node_bootstrap_runner.py",
    "executor/superplane_executor/node_probe_runner.py",
)
EVIDENCE = Path("/opt/superplane/image-evidence")
ENROLLMENT_PATHS = (
    "/run/nodeadm/init",
    "/run/eks/nodeadm/config.json",
    "/etc/eks/kubelet/environment",
    "/etc/kubernetes/kubelet/config.json",
    "/etc/kubernetes/kubelet/config.json.d",
    "/var/lib/kubelet/pki",
    "/var/lib/kubelet/kubeconfig",
    "/var/lib/superplane/node-bootstrap",
    "/etc/eks/superplane-native",
    "/var/lib/amazon/ssm/registration",
)
TREE_ROOTS = {
    "/opt/superplane/node-runtime",
    "/opt/cni/bin",
    "/opt/superplane/dependencies",
}


class ImageRefused(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def exact(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ImageRefused("closed image input required")


def pattern(value, expression):
    if not isinstance(value, str) or not re.fullmatch(expression, value):
        raise ImageRefused("unresolved or invalid image input")


def digest(value):
    pattern(value, r"[a-f0-9]{64}")
    if value == "0" * 64:
        raise ImageRefused("unresolved artifact digest")


def validate_lock(lock):
    exact(
        lock,
        {
            "version",
            "source_revision",
            "source_files",
            "account_id",
            "region",
            "base",
            "builder",
            "tools",
            "bundle_sha256",
            "dependency_review_sha256",
            "runtime",
            "closure",
            "build_timeout_minutes",
            "budget_approval_reference",
        },
    )
    if type(lock["version"]) is not int or lock["version"] != 1:
        raise ImageRefused("unsupported image lock")
    pattern(lock["source_revision"], r"[a-f0-9]{40}")
    pattern(lock["account_id"], r"[0-9]{12}")
    pattern(lock["region"], r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]")
    pattern(lock["budget_approval_reference"], r"[A-Za-z0-9][A-Za-z0-9:/._-]{0,199}")
    if (
        type(lock["build_timeout_minutes"]) is not int
        or not 10 <= lock["build_timeout_minutes"] <= 120
    ):
        raise ImageRefused("bounded build duration required")
    exact(lock["base"], {"ami_id", "owner_id", "architecture"})
    pattern(lock["base"]["ami_id"], r"ami-[a-f0-9]{17}")
    pattern(lock["base"]["owner_id"], r"[0-9]{12}")
    if lock["base"]["architecture"] != "x86_64":
        raise ImageRefused("only reviewed x86_64 native images are supported")
    exact(
        lock["builder"],
        {
            "instance_type",
            "subnet_id",
            "security_group_id",
            "instance_profile",
            "ssh_username",
            "kms_key_id",
        },
    )
    for key, expression in {
        "instance_type": r"[a-z][a-z0-9-]*\.[a-z0-9]+",
        "subnet_id": r"subnet-[a-f0-9]{17}",
        "security_group_id": r"sg-[a-f0-9]{17}",
        "instance_profile": r"[A-Za-z0-9+=,.@_-]{1,128}",
        "ssh_username": r"[a-z_][a-z0-9_-]{0,31}",
        "kms_key_id": r"arn:aws(?:-us-gov)?:kms:[a-z0-9-]+:[0-9]{12}:key/[a-f0-9-]{36}",
    }.items():
        pattern(lock["builder"][key], expression)
    kms = lock["builder"]["kms_key_id"].split(":")
    if kms[3:5] != [lock["region"], lock["account_id"]]:
        raise ImageRefused("image encryption key scope differs")
    exact(lock["tools"], {"packer", "amazon_plugin"})
    for tool in lock["tools"].values():
        exact(tool, {"version", "sha256"})
        pattern(tool["version"], r"[0-9]+\.[0-9]+\.[0-9]+")
        digest(tool["sha256"])
    exact(lock["source_files"], SOURCE_FILES)
    for value in [
        lock["bundle_sha256"],
        lock["dependency_review_sha256"],
        *lock["source_files"].values(),
    ]:
        digest(value)
    exact(lock["runtime"], runner.MANIFEST_FIELDS - {"artifact_sha256"})
    # A derived non-placeholder digest permits use of the real closed validator;
    # the published descriptor receives the actual installed manifest digest.
    runner.validate_runtime_manifest(
        {
            **lock["runtime"],
            "artifact_sha256": sha(runner.canonical(lock["runtime"]).encode()),
        }
    )
    exact(lock["closure"], {"files", "trees"})
    files, trees = lock["closure"]["files"], lock["closure"]["trees"]
    if (
        not isinstance(files, dict)
        or not runner.REQUIRED_FILES <= files.keys()
        or len(files) > 256
    ):
        raise ImageRefused("reviewed executable file closure required")
    if (
        not isinstance(trees, dict)
        or not {str(runner.RUNTIME_ROOT), str(runner.CNI_ROOT)} <= trees.keys()
        or not set(trees) <= TREE_ROOTS
    ):
        raise ImageRefused("reviewed private runtime and CNI trees required")
    for name, value in {**files, **trees}.items():
        path = Path(name)
        if not path.is_absolute() or ".." in path.parts or str(path) != name:
            raise ImageRefused("canonical absolute closure path required")
        if name not in TREE_ROOTS and not name.startswith(
            (
                "/usr/bin/",
                "/usr/lib/",
                "/usr/lib64/",
                "/lib/",
                "/lib64/",
                "/opt/superplane/",
                "/opt/cni/",
            )
        ):
            raise ImageRefused("closure path outside native artifacts")
        digest(value)
    return lock


def read_lock(path):
    raw = Path(path).read_bytes()
    if len(raw) > 131072:
        raise ImageRefused("image input exceeds bound")
    value = runner.decode_json(raw)
    if raw.decode() != runner.canonical(value):
        raise ImageRefused("canonical image input required")
    return validate_lock(value)


def verify_sources(lock, root=APP_ROOT):
    for name, expected in lock["source_files"].items():
        path = root / name
        if (
            path.is_symlink()
            or not path.is_file()
            or sha(path.read_bytes()) != expected
        ):
            raise ImageRefused("maintained native source differs from lock")


def refuse_enrollment():
    if any(os.path.lexists(path) for path in ENROLLMENT_PATHS):
        raise ImageRefused(
            "base contains original or ambiguous enrollment state; never erase it"
        )
    for name in ("/etc/eks/nodeadm.d", "/var/lib/amazon/ssm"):
        path = Path(name)
        if path.is_symlink():
            raise ImageRefused("ambiguous native state directory")
    dropins = Path("/etc/eks/nodeadm.d")
    if dropins.exists() and any(dropins.iterdir()):
        raise ImageRefused("base contains ambient NodeConfig")
    # A native SSM identity is per instance. No builder's registration/command
    # history may be promoted, even though native EC2 registration is not EKS join.
    state = Path("/var/lib/amazon/ssm")
    if state.exists() and any(
        path.name.startswith(("i-", "mi-")) for path in state.iterdir()
    ):
        raise ImageRefused("base contains SSM instance state")


def planned_overlay(archive, lock):
    """Validate the complete archive before writing any member. No tar extraction."""
    files, trees = lock["closure"]["files"], lock["closure"]["trees"]
    entries = []
    seen = set()
    for member in archive.getmembers():
        path = Path("/" + member.name)
        if (
            member.name.startswith("/")
            or ".." in path.parts
            or str(path)[1:] != member.name
            or member.name in seen
        ):
            raise ImageRefused("noncanonical or duplicate bundle member")
        seen.add(member.name)
        if not member.isfile() or member.size > 2 * 1024**3 or member.mode & 0o022:
            raise ImageRefused("bundle requires immutable regular files only")
        if (
            str(path) == str(runner.MANIFEST_PATH)
            or path
            in [
                Path("/opt/superplane/bin") / Path(name).name
                for name in SOURCE_FILES[:2]
            ]
            or path
            in [runner.RUNTIME_ROOT / Path(name).name for name in SOURCE_FILES[2:]]
        ):
            raise ImageRefused(
                "bundle cannot replace maintained source or generated manifest"
            )
        if str(path) not in files and not any(
            path.is_relative_to(tree) for tree in trees
        ):
            raise ImageRefused("bundle member lacks reviewed closure membership")
        entries.append((member, path))
    if not entries or sum(member.size for member, _ in entries) > 16 * 1024**3:
        raise ImageRefused("empty or oversized runtime bundle")
    return entries


def directory(path):
    path = Path(path)
    if path.exists() or path.is_symlink():
        return runner.secure_path(path, directory=True)
    directory(path.parent)
    path.mkdir(mode=0o755)
    return runner.secure_path(path, directory=True)


def install_file(path, content, executable):
    directory(path.parent)
    if path.exists() or path.is_symlink():
        runner.secure_path(path)
        if path.read_bytes() == content:
            return
        # This runs only on a verified never-enrolled builder. Replacement is
        # explicit reviewed image preparation, never an allocated-node repair.
    temporary = path.with_name(path.name + ".superplane-image-new")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o755 if executable else 0o644,
    )
    with os.fdopen(fd, "wb") as destination:
        destination.write(content)
        destination.flush()
        os.fsync(destination.fileno())
    os.replace(temporary, path)


def manifest(lock):
    for group, measure in (
        ("files", runner.file_digest),
        ("trees", runner.tree_digest),
    ):
        for name, expected in lock["closure"][group].items():
            if measure(name) != expected:
                raise ImageRefused(
                    "installed artifact differs from reviewed closure: " + name
                )
    value = {"version": 1, "runtime": lock["runtime"], **lock["closure"]}
    raw = runner.canonical(value).encode()
    if len(raw) > 65536:
        raise ImageRefused("installed manifest exceeds runtime bound")
    descriptor = {
        "version": 1,
        "runtime_manifest": {**lock["runtime"], "artifact_sha256": sha(raw)},
        "bootstrap_wrapper_sha256": runner.file_digest(
            runner.RUNTIME_ROOT / "node_bootstrap_runner.py"
        ),
        "probe_wrapper_sha256": runner.file_digest(
            runner.RUNTIME_ROOT / "node_probe_runner.py"
        ),
    }
    runner.validate_runtime_manifest(descriptor["runtime_manifest"])
    if len(runner.canonical(descriptor)) > 2000:
        raise ImageRefused("native descriptor exceeds approved parameter bound")
    return raw, descriptor


def verify_prepared(lock):
    refuse_enrollment()
    bootstrap.verify_bootstrap_exclusive()
    raw, descriptor = manifest(lock)
    if runner.MANIFEST_PATH.read_bytes() != raw:
        raise ImageRefused("prepared manifest changed")
    for purpose, name, field in (
        ("node-bootstrap", "node_bootstrap_runner.py", "bootstrap_wrapper_sha256"),
        ("node-api-dns-tls", "node_probe_runner.py", "probe_wrapper_sha256"),
    ):
        contract = {
            "purpose": purpose,
            "runtime_manifest": descriptor["runtime_manifest"],
            "wrapper_sha256": descriptor[field],
        }
        runner.verify_installation(contract, runner.RUNTIME_ROOT / name)
        # Verify the real isolated interpreter/import closure, without invoking
        # IMDS, nodeadm, SSM, Kubernetes or the node-side mutation entry points.
        subprocess.run(
            [
                str(runner.RUNTIME_ROOT / "bin/python3"),
                "-I",
                "-B",
                "-S",
                "-c",
                "import sys,json; assert sys.version_info[:2] == (3,12); "
                "sys.path.insert(0,'/opt/superplane/node-runtime'); "
                "import node_runner,node_bootstrap_runner,node_probe_runner; "
                "node_runner.verify_installation(json.loads(sys.argv[1]),sys.argv[2])",
                runner.canonical(contract),
                str(runner.RUNTIME_ROOT / name),
            ],
            check=True,
            timeout=30,
            env=runner.clean_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    return descriptor


def prepare(lock, bundle):
    if os.geteuid() != 0:
        raise ImageRefused("image preparation requires an isolated root builder")
    verify_sources(lock)
    refuse_enrollment()
    # No stop/reset/delete of an existing execution. Refuse active bootstrap
    # before masking; revalidate the full maintained exclusivity guard after it.
    for unit in ("nodeadm-config.service", "nodeadm-run.service", "kubelet.service"):
        state = runner.fixed_command(
            ["/usr/bin/systemctl", "show", unit, "--property=ActiveState", "--value"]
        ).strip()
        if state != "inactive":
            raise ImageRefused("base bootstrap is active or ambiguous")
    if sha(Path(bundle).read_bytes()) != lock["bundle_sha256"]:
        raise ImageRefused("runtime input bundle differs")
    with tarfile.open(bundle, mode="r:*") as archive:
        entries = planned_overlay(archive, lock)
        subprocess.run(
            [
                "/usr/bin/systemctl",
                "mask",
                "nodeadm-config.service",
                "nodeadm-run.service",
            ],
            check=True,
        )
        bootstrap.verify_bootstrap_exclusive()
        for member, path in entries:
            with archive.extractfile(member) as source:
                install_file(path, source.read(), bool(member.mode & 0o111))
    for name in SOURCE_FILES:
        target = (
            Path("/opt/superplane/bin")
            if "node-command/" in name
            else runner.RUNTIME_ROOT
        ) / Path(name).name
        install_file(target, (APP_ROOT / name).read_bytes(), "node-command/" in name)
    directory(Path("/var/lib/superplane"))
    directory(Path("/etc/eks"))
    raw, descriptor = manifest(lock)
    install_file(runner.MANIFEST_PATH, raw, False)
    verify_prepared(lock)
    directory(EVIDENCE)
    install_file(
        EVIDENCE / "descriptor.json", runner.canonical(descriptor).encode(), False
    )
    install_file(EVIDENCE / "input-lock.json", runner.canonical(lock).encode(), False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("validate", "prepare", "verify"))
    parser.add_argument("--lock", required=True)
    parser.add_argument("--bundle")
    args = parser.parse_args()
    lock = read_lock(args.lock)
    if args.action == "prepare":
        if not args.bundle:
            parser.error("prepare requires --bundle")
        prepare(lock, args.bundle)
    elif args.action == "verify":
        verify_prepared(lock)
    else:
        verify_sources(lock)


if __name__ == "__main__":
    main()
