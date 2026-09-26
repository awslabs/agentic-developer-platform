"""Prepared-image artifacts must satisfy the actual fixed native runtime verifier."""

import copy
import importlib.util
import io
from pathlib import Path
import sys
import tarfile

import pytest

from superplane_executor import node_command_plan, node_runner as runner

ROOT = Path(__file__).resolve().parents[2] / "images/native-node"
spec = importlib.util.spec_from_file_location(
    "superplane_native_image_recipe", ROOT / "native_image.py"
)
recipe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recipe)


def runtime():
    # Deliberately test-only values, never a published image lock or example AMI.
    return {
        "version": 1,
        "nodeadm_commit": runner.NODEADM_COMMIT,
        "architecture": "x86_64",
        "kubelet_version": "v1.34.1",
        "containerd_version": "2.1.0",
        "cni": "aws-vpc-cni",
        "cni_version": "v1.20.0",
        "nvidia_driver_version": "580.65.06",
        "nvidia_runtime_version": "1.17.8",
        "device_plugin_image": "sha256:" + "a" * 64,
        "ssm_agent_version": "3.3.3050.0",
    }


@pytest.fixture
def lock():
    return {
        "version": 1,
        "source_revision": "b" * 40,
        "source_files": {name: "a" * 64 for name in recipe.SOURCE_FILES},
        "account_id": "123456789012",
        "region": "us-west-2",
        "base": {
            "ami_id": "ami-0123456789abcdef0",
            "owner_id": "123456789012",
            "architecture": "x86_64",
        },
        "builder": {
            "instance_type": "m6i.large",
            "subnet_id": "subnet-0123456789abcdef0",
            "security_group_id": "sg-0123456789abcdef0",
            "instance_profile": "reviewed-builder",
            "ssh_username": "ec2-user",
            "kms_key_id": "arn:aws:kms:us-west-2:123456789012:key/11111111-1111-1111-1111-111111111111",
        },
        "tools": {
            name: {"version": "1.0.0", "sha256": "b" * 64}
            for name in ("packer", "amazon_plugin")
        },
        "bundle_sha256": "c" * 64,
        "dependency_review_sha256": "d" * 64,
        "runtime": runtime(),
        "closure": {
            "files": {name: "e" * 64 for name in runner.REQUIRED_FILES},
            "trees": {
                str(runner.RUNTIME_ROOT): "f" * 64,
                str(runner.CNI_ROOT): "a" * 64,
            },
        },
        "build_timeout_minutes": 30,
        "budget_approval_reference": "test-only-approval",
    }


@pytest.mark.parametrize(
    "change", ["placeholder", "foreign-kms", "source", "runtime", "unbounded", "escape"]
)
def test_unresolved_or_widened_lock_is_refused(lock, change):
    recipe.validate_lock(lock)
    if change == "placeholder":
        lock["bundle_sha256"] = "0" * 64
    elif change == "foreign-kms":
        lock["builder"]["kms_key_id"] = lock["builder"]["kms_key_id"].replace(
            "us-west-2", "us-east-1"
        )
    elif change == "source":
        del lock["source_files"][recipe.SOURCE_FILES[0]]
    elif change == "runtime":
        lock["runtime"]["nodeadm_commit"] = "f" * 40
    elif change == "unbounded":
        lock["build_timeout_minutes"] = 0
    else:
        lock["closure"]["files"]["/etc/shadow"] = "a" * 64
    with pytest.raises((recipe.ImageRefused, runner.RunnerRefused)):
        recipe.validate_lock(lock)


@pytest.mark.parametrize(
    "kind",
    ["traversal", "symlink", "hardlink", "duplicate", "source", "outside", "writable"],
)
def test_input_bundle_cannot_escape_or_replace_maintained_code(lock, kind):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        member = tarfile.TarInfo("opt/superplane/node-runtime/bin/python3")
        member.size = 3
        member.mode = 0o755
        if kind == "traversal":
            member.name = "opt/../etc/shadow"
        elif kind in {"symlink", "hardlink"}:
            member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            member.linkname = "/etc/shadow"
            member.size = 0
        elif kind == "source":
            member.name = "opt/superplane/node-runtime/node_runner.py"
        elif kind == "outside":
            member.name = "etc/shadow"
        elif kind == "writable":
            member.mode = 0o777
        archive.addfile(member, io.BytesIO(b"bin"))
        if kind == "duplicate":
            archive.addfile(member, io.BytesIO(b"bin"))
    buffer.seek(0)
    with tarfile.open(fileobj=buffer) as archive, pytest.raises(recipe.ImageRefused):
        recipe.planned_overlay(archive, lock)


def test_existing_enrollment_refuses_before_mask_install_or_erase(
    monkeypatch, tmp_path, lock
):
    marker = tmp_path / "original-latch"
    marker.write_bytes(b"original uncertain init")
    monkeypatch.setattr(recipe, "ENROLLMENT_PATHS", (str(marker),))
    monkeypatch.setattr(recipe, "verify_sources", lambda _: None)
    monkeypatch.setattr(recipe.os, "geteuid", lambda: 0)

    def forbidden(*args, **kwargs):
        pytest.fail("an enrolled base must refuse before process or mutation")

    monkeypatch.setattr(recipe.subprocess, "run", forbidden)
    with pytest.raises(recipe.ImageRefused, match="enrollment state"):
        recipe.prepare(lock, tmp_path / "does-not-exist.tar")
    assert marker.read_bytes() == b"original uncertain init"


@pytest.fixture
def installed(monkeypatch, tmp_path):
    root, cni = tmp_path / "runtime", tmp_path / "cni"
    root.mkdir()
    cni.mkdir()
    for name in ("node_runner.py", "node_bootstrap_runner.py", "node_probe_runner.py"):
        (root / name).write_bytes(
            (recipe.APP_ROOT / "executor/superplane_executor" / name).read_bytes()
        )
    (cni / "aws-cni").write_bytes(b"test-only CNI artifact")
    binary = tmp_path / "native-binary"
    binary.write_bytes(b"test-only binary")
    monkeypatch.setattr(runner, "RUNTIME_ROOT", root)
    monkeypatch.setattr(runner, "CNI_ROOT", cni)
    monkeypatch.setattr(runner, "MANIFEST_PATH", root / "manifest.json")
    monkeypatch.setattr(runner, "REQUIRED_FILES", {str(binary)})
    # Preserve real hashing, manifest parsing and descriptor validation. Only
    # disposable CI ownership/absolute install locations differ from an AMI.
    monkeypatch.setattr(runner, "secure_path", lambda p, **_: Path(p))
    monkeypatch.setattr(runner.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner.platform, "machine", lambda: "x86_64")
    return {
        "runtime": runtime(),
        "closure": {
            "files": {str(binary): runner.file_digest(binary)},
            "trees": {
                str(root): runner.tree_digest(root),
                str(cni): runner.tree_digest(cni),
            },
        },
    }


@pytest.mark.parametrize(
    "drift", [None, "extra-file", "wrapper", "manifest", "descriptor"]
)
def test_generated_artifact_is_consumed_by_real_native_verifiers(installed, drift):
    raw, descriptor = recipe.manifest(installed)
    node_command_plan.validate(descriptor)
    runner.MANIFEST_PATH.write_bytes(raw)
    contract = {
        "purpose": "node-bootstrap",
        "runtime_manifest": descriptor["runtime_manifest"],
        "wrapper_sha256": descriptor["bootstrap_wrapper_sha256"],
    }
    wrapper = runner.RUNTIME_ROOT / "node_bootstrap_runner.py"
    runner.verify_installation(contract, wrapper)
    if drift is None:
        assert recipe.manifest(installed) == (raw, descriptor)
        return
    if drift == "extra-file":
        (runner.RUNTIME_ROOT / "unreviewed.py").write_bytes(b"ambient module")
    elif drift == "wrapper":
        wrapper.write_bytes(b"unreviewed wrapper")
    elif drift == "manifest":
        runner.MANIFEST_PATH.write_bytes(raw + b"\n")
    else:
        contract = copy.deepcopy(contract)
        contract["runtime_manifest"]["artifact_sha256"] = "b" * 64
    with pytest.raises(runner.RunnerRefused):
        runner.verify_installation(contract, wrapper)


@pytest.fixture
def build_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "native_image", recipe)
    build_spec = importlib.util.spec_from_file_location(
        "superplane_native_image_build", ROOT / "build.py"
    )
    build = importlib.util.module_from_spec(build_spec)
    build_spec.loader.exec_module(build)
    return build


def test_prepublication_check_uses_both_real_wrappers_without_bootstrap(
    installed, monkeypatch
):
    raw, descriptor = recipe.manifest(installed)
    runner.MANIFEST_PATH.write_bytes(raw)
    monkeypatch.setattr(recipe, "refuse_enrollment", lambda: None)
    monkeypatch.setattr(recipe.bootstrap, "verify_bootstrap_exclusive", lambda: None)
    calls = []

    def isolated_import(arguments, **kwargs):
        assert arguments[0] == str(runner.RUNTIME_ROOT / "bin/python3")
        assert arguments[1:5] == ["-I", "-B", "-S", "-c"]
        assert "verify_installation" in arguments[5]
        assert "execute(" not in arguments[5]
        assert kwargs["env"] == runner.clean_environment()
        assert kwargs["timeout"] == 30
        calls.append(arguments)

    monkeypatch.setattr(recipe.subprocess, "run", isolated_import)
    assert recipe.verify_prepared(installed) == descriptor
    assert len(calls) == 2
    assert calls[0][-1].endswith("node_bootstrap_runner.py")
    assert calls[1][-1].endswith("node_probe_runner.py")


@pytest.mark.parametrize("wrong", ["account", "base-owner", "architecture", "state"])
def test_actual_target_preflight_refuses_wrong_provenance(
    build_module, lock, monkeypatch, wrong
):
    calls = []

    def response(current, *arguments):
        calls.append(arguments)
        if arguments[0] == "sts":
            return {
                "Account": "000000000001" if wrong == "account" else lock["account_id"]
            }
        base = {
            "OwnerId": lock["base"]["owner_id"],
            "Architecture": "x86_64",
            "State": "available",
            "RootDeviceType": "ebs",
            "VirtualizationType": "hvm",
        }
        base.update(
            {
                "base-owner": {"OwnerId": "000000000001"},
                "architecture": {"Architecture": "arm64"},
                "state": {"State": "pending"},
            }.get(wrong, {})
        )
        return {"Images": [base]}

    monkeypatch.setattr(build_module, "aws", response)
    with pytest.raises(recipe.ImageRefused):
        build_module.check_target(lock)
    assert len(calls) == (1 if wrong == "account" else 2)


def test_changed_tool_refuses_before_it_can_execute(build_module, tmp_path):
    binary = tmp_path / "packer"
    binary.write_bytes(b"unreviewed executable")
    with pytest.raises(recipe.ImageRefused, match="tool digest"):
        build_module.verify_tool(binary, {"sha256": "a" * 64})


@pytest.mark.parametrize("change", ["extra", "missing", "unencrypted", "foreign-key"])
def test_only_exact_ami_snapshots_can_be_retained(build_module, lock, change):
    result = {"BlockDeviceMappings": [{"Ebs": {"SnapshotId": "snap-original"}}]}
    observed = [
        {
            "SnapshotId": "snap-original",
            "OwnerId": lock["account_id"],
            "Encrypted": True,
            "KmsKeyId": lock["builder"]["kms_key_id"],
        }
    ]
    build_module.require_snapshot_provenance(lock, result, observed)
    if change == "extra":
        observed.append({**observed[0], "SnapshotId": "snap-intermediate"})
    elif change == "missing":
        observed.clear()
    elif change == "unencrypted":
        observed[0]["Encrypted"] = False
    else:
        observed[0]["KmsKeyId"] = "foreign-key"
    with pytest.raises(recipe.ImageRefused):
        build_module.require_snapshot_provenance(lock, result, observed)


def test_bundle_hash_and_extraction_use_bounded_reads(monkeypatch, tmp_path):
    content = b"runtime-closure" * 300000
    archive = tmp_path / "runtime.tar"
    archive.write_bytes(content)

    def forbidden(*args, **kwargs):
        pytest.fail("large runtime inputs must not use read_bytes")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert recipe.file_sha(archive) == recipe.sha(content)
    monkeypatch.setattr(recipe, "directory", lambda path: path)
    monkeypatch.setattr(runner, "secure_path", lambda path, **kwargs: path)

    class BoundedSource(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 1024 * 1024
            return super().read(size)

    installed = tmp_path / "installed"
    recipe.install_stream(installed, BoundedSource(content), executable=True)
    assert recipe.file_sha(installed) == recipe.sha(content)
    recipe.install_stream(installed, BoundedSource(content), executable=True)
    assert not installed.with_name("installed.superplane-image-new").exists()


def test_wrong_bundle_digest_refuses_before_mutation(monkeypatch, tmp_path, lock):
    bundle = tmp_path / "runtime.tar"
    bundle.write_bytes(b"unreviewed archive")
    monkeypatch.setattr(recipe, "verify_sources", lambda _: None)
    monkeypatch.setattr(recipe, "refuse_enrollment", lambda: None)
    monkeypatch.setattr(recipe.os, "geteuid", lambda: 0)
    monkeypatch.setattr(runner, "fixed_command", lambda _: "inactive")

    def forbidden(*args, **kwargs):
        pytest.fail("wrong digest must refuse before mask, install or archive opening")

    monkeypatch.setattr(recipe.subprocess, "run", forbidden)
    monkeypatch.setattr(recipe.tarfile, "open", forbidden)
    monkeypatch.setattr(recipe, "install_stream", forbidden)
    with pytest.raises(recipe.ImageRefused, match="bundle differs"):
        recipe.prepare(lock, bundle)


def test_temporary_resources_cannot_be_reported_clean(build_module):
    build = build_module
    evidence = {"instances": [], "volumes": [], "key_pairs": []}
    build.require_cleanup(evidence)
    for kind in ("instances", "volumes", "key_pairs"):
        current = copy.deepcopy(evidence)
        current[kind] = (
            [{"Instances": [{"State": {"Name": "running"}}]}]
            if kind == "instances"
            else [{"owned": "remaining"}]
        )
        with pytest.raises(recipe.ImageRefused, match="resources remain"):
            build.require_cleanup(current)
