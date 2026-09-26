"""Prepared-image artifacts must satisfy the actual fixed native runtime verifier."""

import copy
import importlib.util
import io
import json
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


@pytest.mark.parametrize("inventory_available", [False, True])
def test_interrupted_build_retains_original_identity_and_cleanup_state(
    build_module, tmp_path, lock, monkeypatch, inventory_available
):
    state = {
        "build_id": "original-build",
        "phase": "preflight",
        "cleanup": "not_started",
    }
    build_module.write(tmp_path / "state.json", state)

    class InterruptedProcess:
        pid = 123
        calls = 0

        def wait(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise build_module.subprocess.TimeoutExpired("packer", 30)
            return 0

    def start(*args, **kwargs):
        before = json.loads((tmp_path / "state.json").read_text())
        assert before["phase"] == "building"
        assert before["cleanup"] == "unknown"
        return InterruptedProcess()

    monkeypatch.setattr(build_module.subprocess, "Popen", start)
    monkeypatch.setattr(build_module.os, "killpg", lambda *args: None)

    def observe(*args):
        assert (
            json.loads((tmp_path / "state.json").read_text())["phase"]
            == "reconciling_cleanup"
        )
        if not inventory_available:
            raise ConnectionError("inventory unavailable")
        return {"instances": [{"original": "still-unknown"}]}

    monkeypatch.setattr(build_module, "inventory", observe)
    with pytest.raises((recipe.ImageRefused, ConnectionError)):
        build_module.complete_build(
            lock,
            tmp_path,
            tmp_path / "recipe",
            {},
            "original-build",
            "packer",
            {},
            state,
        )
    final = json.loads((tmp_path / "state.json").read_text())
    assert final["phase"] == "failed" and final["build_id"] == "original-build"
    assert final["build_outcome"] == "failed"
    assert final["cleanup"] == (
        "inventory_recorded" if inventory_available else "unknown"
    )
    assert not (tmp_path / "result.json").exists()


def test_interrupted_metadata_replace_preserves_previous_durable_state(
    build_module, tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    build_module.write(target, {"phase": "building", "cleanup": "unknown"})

    def interrupted(*args):
        raise OSError("simulated interrupted replacement")

    monkeypatch.setattr(build_module.os, "replace", interrupted)
    with pytest.raises(OSError):
        build_module.write(target, {"phase": "complete"})
    assert json.loads(target.read_text()) == {"phase": "building", "cleanup": "unknown"}
    assert list(tmp_path.iterdir()) == [target]


@pytest.fixture
def producer_modules(build_module, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    monkeypatch.setitem(sys.modules, "build", build_module)
    loaded = {}
    for name in ("source_provenance", "upstream", "producer", "offline_root"):
        module_spec = importlib.util.spec_from_file_location(
            name, ROOT / (name + ".py")
        )
        module = importlib.util.module_from_spec(module_spec)
        monkeypatch.setitem(sys.modules, name, module)
        module_spec.loader.exec_module(module)
        loaded[name] = module
    return loaded


@pytest.fixture
def producer_plan(lock):
    value = {
        key: copy.deepcopy(lock[key])
        for key in (
            "version",
            "source_revision",
            "source_files",
            "account_id",
            "region",
            "builder",
            "tools",
            "runtime",
            "build_timeout_minutes",
            "budget_approval_reference",
        )
    }
    value["source_attestation_sha256"] = "a" * 64
    value["helper"] = {key: lock["base"][key] for key in ("ami_id", "owner_id")}
    value["target"] = {
        **value["helper"],
        "snapshot_id": "snap-0123456789abcdef0",
        "root_device_name": "/dev/xvda",
        "boot_mode": "uefi-preferred",
        "ena_support": True,
    }
    value["builder"]["target_volume_size_gb"] = 40
    value["tools"]["amazon_plugin"]["version"] = "1.8.2"
    value["extra_runtime_files"] = ["/usr/bin/runc"]
    value["upstream"] = {
        key: {"file": key + ".tar.gz", "sha256": "a" * 64, "prefix": "."}
        for key in ("python", "nodeadm", "crictl")
    }
    value["upstream"]["nodeadm"]["prefix"] = "amazon-eks-ami-" + runner.NODEADM_COMMIT
    value["upstream"].update(
        go_image="registry.example/go@sha256:" + "b" * 64,
        cni_image="registry.example/cni@sha256:" + "c" * 64,
        cni_files={"/app/aws-cni": "aws-cni"},
    )
    return value


def test_surrogate_produces_target_root_without_helper_or_copy(
    producer_modules, producer_plan, tmp_path
):
    producer = producer_modules["producer"]
    producer.validate_plan(producer_plan)
    document = producer.template(
        producer_plan, tmp_path / "source", tmp_path, "unique-build"
    )
    source = document["source"]["amazon-ebssurrogate"]["native"]
    assert source["source_ami"] == producer_plan["helper"]["ami_id"]
    assert source["ami_root_device"] == {
        "source_device_name": "/dev/sdf",
        "device_name": "/dev/xvda",
    }
    assert len(source["launch_block_device_mappings"]) == 1
    target = source["launch_block_device_mappings"][0]
    assert target["snapshot_id"] == producer_plan["target"]["snapshot_id"]
    assert target["delete_on_termination"] and target["encrypted"]
    assert target["kms_key_id"] == producer_plan["builder"]["kms_key_id"]
    assert "encrypt_boot" not in source and "kms_key_id" not in source
    assert source["boot_mode"] == "uefi-preferred" and source["ena_support"]
    assert source["ami_virtualization_type"] == "hvm"


def test_alias_materialization_bounds_actual_copied_bytes(
    producer_modules, tmp_path, monkeypatch
):
    upstream = producer_modules["upstream"]
    archive = tmp_path / "python.tar"
    with tarfile.open(archive, "w") as output:
        regular = tarfile.TarInfo("python/runtime")
        regular.size = 10
        output.addfile(regular, io.BytesIO(b"0123456789"))
        for number in range(3):
            link = tarfile.TarInfo("python/alias" + str(number))
            link.type, link.linkname = tarfile.LNKTYPE, "python/runtime"
            output.addfile(link)
    monkeypatch.setattr(upstream, "MAX_EMITTED_BYTES", 20)
    with pytest.raises(recipe.ImageRefused, match="materialized upstream output"):
        upstream.unpack_regular(archive, "python", tmp_path / "output")
    assert sum(path.stat().st_size for path in (tmp_path / "output").iterdir()) <= 20


def test_wrong_vendored_nodeadm_source_is_not_a_build_input(producer_modules, tmp_path):
    source = tmp_path / "source"
    (source / "vendor").mkdir(parents=True)
    (source / "Makefile").write_text("release: fabricated\n")
    (source / "vendor/modules.txt").write_text("unreviewed dependencies\n")
    with pytest.raises(recipe.ImageRefused, match="pinned upstream"):
        producer_modules["upstream"].verify_nodeadm_source(source)


def test_absolute_guest_alias_never_resolves_against_helper(producer_modules, tmp_path):
    root = tmp_path / "target"
    (root / "etc").mkdir(parents=True)
    (root / "etc/guest").symlink_to("/private/value")
    offline = producer_modules["offline_root"]
    assert offline.target_path(root, "/etc/guest") == root / "private/value"
    (root / "etc/cycle").symlink_to("/etc/cycle")
    with pytest.raises(recipe.ImageRefused, match="cycle"):
        offline.target_path(root, "/etc/cycle")


def test_overlay_cannot_follow_parent_alias_into_helper(producer_modules, tmp_path):
    offline = producer_modules["offline_root"]
    root, helper, source = (tmp_path / name for name in ("target", "helper", "input"))
    for path in (root, helper, source):
        path.mkdir()
    (root / "opt").symlink_to(helper, target_is_directory=True)
    (source / "data").write_bytes(b"artifact")
    with pytest.raises(recipe.ImageRefused, match="parent.*alias"):
        offline.install_tree(source, root, "/opt/superplane")
    assert list(helper.iterdir()) == []


def test_offline_original_enrollment_is_preserved(producer_modules, tmp_path):
    original = tmp_path / "var/lib/kubelet/kubeconfig"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original identity")
    with pytest.raises(recipe.ImageRefused, match="enrollment state"):
        producer_modules["offline_root"].refuse_enrollment(tmp_path)
    assert original.read_bytes() == b"original identity"


@pytest.mark.parametrize(
    "wrong", ["snapshot", "instance", "tag", "key", "retained", "serial", "mounted"]
)
def test_target_volume_identity_precedes_any_mount(
    producer_modules, producer_plan, wrong
):
    offline = producer_modules["offline_root"]
    instance = "i-0123456789abcdef0"
    volume = {
        "VolumeId": "vol-0123456789abcdef0",
        "SnapshotId": producer_plan["target"]["snapshot_id"],
        "Encrypted": True,
        "KmsKeyId": producer_plan["builder"]["kms_key_id"],
        "State": "in-use",
        "Tags": [
            {"Key": "superplane-native-build", "Value": "build"},
            {"Key": "superplane-source", "Value": producer_plan["source_revision"]},
        ],
        "Attachments": [
            {
                "InstanceId": instance,
                "Device": "/dev/sdf",
                "State": "attached",
                "DeleteOnTermination": True,
            }
        ],
    }
    device = {
        "name": "/dev/nvme9n1",
        "type": "disk",
        "serial": volume["VolumeId"].replace("-", ""),
        "mountpoints": [None],
        "children": [
            {
                "name": "/dev/nvme9n1p1",
                "type": "part",
                "fstype": "xfs",
                "mountpoints": [None],
            }
        ],
    }
    assert offline.bound_volume(producer_plan, "build", instance, [volume]) == volume
    assert (
        offline.device_for(volume["VolumeId"], [device])[0]["name"] == "/dev/nvme9n1p1"
    )
    if wrong == "snapshot":
        volume["SnapshotId"] = "foreign"
    elif wrong == "instance":
        volume["Attachments"][0]["InstanceId"] = "foreign"
    elif wrong == "tag":
        volume["Tags"][0]["Value"] = "foreign"
    elif wrong == "key":
        volume["KmsKeyId"] = "foreign"
    elif wrong == "retained":
        volume["Attachments"][0]["DeleteOnTermination"] = False
    elif wrong == "serial":
        device["serial"] = "volforeign"
    else:
        device["children"][0]["mountpoints"] = ["/"]
    with pytest.raises(recipe.ImageRefused):
        offline.bound_volume(producer_plan, "build", instance, [volume])
        offline.device_for(volume["VolumeId"], [device])


def test_archive_source_attestation_binds_git_tree_without_git_metadata(
    producer_modules, tmp_path
):
    import hashlib
    import os
    import shutil
    import subprocess

    provenance = producer_modules["source_provenance"]
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    nested = checkout / "nested"
    nested.mkdir()
    (nested / "tool").write_text("#!/bin/sh\nexit 0\n")
    (nested / "tool").chmod(0o755)
    (checkout / "nested.txt").write_text("tree ordering matters\n")
    os.symlink("nested/tool", checkout / "alias")
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture",
        ],
        check=True,
        capture_output=True,
    )
    revision = provenance.git(checkout, "rev-parse", "HEAD").decode().strip()
    value = provenance.create(checkout, revision)
    attestation = tmp_path / "attestation.json"
    attestation.write_text(json.dumps(value, sort_keys=True))
    digest = hashlib.sha256(attestation.read_bytes()).hexdigest()
    shutil.rmtree(checkout / ".git")
    receipt = provenance.verify(checkout, attestation, digest, revision)
    assert receipt["tree"] == value["tree"]
    assert receipt["files"] == 3
    (nested / "tool").chmod(0o644)
    with pytest.raises(recipe.ImageRefused, match="source differs"):
        provenance.verify(checkout, attestation, digest, revision)
    (nested / "tool").chmod(0o755)
    (checkout / "untracked").write_text("unexpected executable")
    with pytest.raises(recipe.ImageRefused, match="inventory/tree"):
        provenance.verify(checkout, attestation, digest, revision)
    (checkout / "untracked").unlink()
    (checkout / "alias").unlink()
    os.symlink("/etc/passwd", checkout / "alias")
    with pytest.raises(recipe.ImageRefused, match="source differs"):
        provenance.verify(checkout, attestation, digest, revision)


@pytest.fixture
def native_transport(producer_modules, monkeypatch):
    path = ROOT / "lane" / "transport.py"
    module_spec = importlib.util.spec_from_file_location("native_transport", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_lane_omits_image_tags_but_binds_atomic_resources_to_caller(
    producer_modules, producer_plan, tmp_path
):
    template = producer_modules["producer"].template(
        producer_plan,
        tmp_path / "source",
        tmp_path,
        "build",
        caller="AROAFIXTURE:build-one",
    )
    source = template["source"]["amazon-ebssurrogate"]["native"]
    assert source["tags"] == {}
    for key in ("run_tags", "run_volume_tags", "snapshot_tags"):
        assert source[key]["superplane-native-caller"] == "AROAFIXTURE:build-one"
        assert source[key]["superplane-native-build"] == "build"


@pytest.mark.parametrize("bad", ["../outside", "/absolute", "alias/child"])
def test_native_source_zip_refuses_escape_before_writes(
    native_transport, tmp_path, bad
):
    import stat
    import zipfile

    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        alias = zipfile.ZipInfo("alias")
        alias.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(alias, "/tmp")
        output.writestr(bad, "untrusted")
    destination = tmp_path / "extracted"
    with pytest.raises(recipe.ImageRefused):
        native_transport.extract(archive, destination)
    assert not destination.exists()


def test_native_source_zip_preserves_modes_and_bounds_emitted_size(
    native_transport, tmp_path, monkeypatch
):
    import stat
    import zipfile

    archive = tmp_path / "source.zip"
    with zipfile.ZipFile(archive, "w") as output:
        tool = zipfile.ZipInfo("nested/tool")
        tool.external_attr = (stat.S_IFREG | 0o755) << 16
        output.writestr(tool, "#!/bin/sh\n")
        alias = zipfile.ZipInfo("alias")
        alias.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(alias, "nested/tool")
    destination = tmp_path / "extracted"
    native_transport.extract(archive, destination)
    assert (destination / "nested/tool").stat().st_mode & 0o111
    assert (destination / "alias").is_symlink()
    monkeypatch.setattr(native_transport, "MAX_ARCHIVE_BYTES", 1)
    with pytest.raises(recipe.ImageRefused, match="bound"):
        native_transport.extract(archive, tmp_path / "over-limit")
    assert not (tmp_path / "over-limit").exists()


def test_native_versioned_transport_refuses_digest_drift(
    native_transport, tmp_path, monkeypatch
):
    def fake_aws(region, *args):
        Path(args[-1]).write_bytes(b"wrong bytes")
        return {"VersionId": "original-version"}

    monkeypatch.setattr(native_transport, "aws", fake_aws)
    pointer = {
        "bucket": "fixture-native-input",
        "key": "native-input/run/input",
        "version": "original-version",
        "sha256": "a" * 64,
    }
    with pytest.raises(recipe.ImageRefused, match="digest differs"):
        native_transport.download(
            "us-east-1", pointer, tmp_path / "input", "fixture-native-input"
        )
    pointer["version"] = "null"
    with pytest.raises(recipe.ImageRefused, match="scope/version"):
        native_transport.download(
            "us-east-1", pointer, tmp_path / "input", "fixture-native-input"
        )


@pytest.fixture
def native_dispatch(native_transport, monkeypatch):
    monkeypatch.setitem(sys.modules, "transport", native_transport)
    path = ROOT / "lane" / "dispatch.py"
    module_spec = importlib.util.spec_from_file_location("native_dispatch", path)
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


@pytest.fixture
def approved_native_project():
    return {
        "name": "fixture-native",
        "serviceRole": "arn:aws:iam::111122223333:role/native-build",
        "environment": {
            "image": "image@sha256:" + "a" * 64,
            "type": "LINUX_CONTAINER",
            "computeType": "BUILD_GENERAL1_MEDIUM",
            "privilegedMode": True,
            "imagePullCredentialsType": "CODEBUILD",
            "environmentVariables": [
                {
                    "name": "NATIVE_ACCOUNT_ID",
                    "value": "111122223333",
                    "type": "PLAINTEXT",
                }
            ],
        },
        "vpcConfig": {
            "vpcId": "vpc-original",
            "subnets": ["subnet-original"],
            "securityGroupIds": ["sg-original"],
        },
        "timeoutInMinutes": 150,
        "queuedTimeoutInMinutes": 30,
        "concurrentBuildLimit": 1,
        "source": {
            "type": "S3",
            "location": "native-input/source.zip",
            "buildspec": "modules/domain-apps/superplane/releases/buildspecs/native-node-lane.yml",
        },
        "artifacts": {"type": "NO_ARTIFACTS"},
    }


@pytest.mark.parametrize(
    "field",
    [
        "serviceRole",
        "image",
        "vpcId",
        "subnets",
        "securityGroupIds",
        "computeType",
        "privilegedMode",
        "concurrentBuildLimit",
        "timeoutInMinutes",
        "queuedTimeoutInMinutes",
    ],
)
def test_native_project_drift_refuses_unchanged_name_and_buildspec(
    native_dispatch, approved_native_project, field
):
    original = approved_native_project
    deployment = {
        "project_name": original["name"],
        "project": native_dispatch.project_view(original),
    }
    observed = copy.deepcopy(original)
    if field in observed["environment"]:
        observed["environment"][field] = (
            False if field == "privilegedMode" else "changed"
        )
    elif field in observed["vpcConfig"]:
        observed["vpcConfig"][field] = ["changed"] if field != "vpcId" else "changed"
    else:
        observed[field] = "changed"
    with pytest.raises(recipe.ImageRefused, match="approved lane deployment"):
        native_dispatch.verify_project(observed, deployment)


def test_native_plan_and_artifact_mutation_cannot_replace_approved_upload(
    native_transport, producer_plan, tmp_path, monkeypatch
):
    import hashlib

    path = tmp_path / "plan.json"
    raw = runner.canonical(producer_plan).encode()
    path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    native_transport.approved_plan(path, digest)
    changed = copy.deepcopy(producer_plan)
    changed["budget_approval_reference"] = "changed-after-review"
    changed["build_timeout_minutes"] = 120
    path.write_text(runner.canonical(changed))
    with pytest.raises(recipe.ImageRefused, match="approved plan digest"):
        native_transport.approved_plan(path, digest)
    with pytest.raises(recipe.ImageRefused, match="staged input"):
        native_transport.freeze(path, tmp_path / "frozen" / "plan.json", digest)

    artifact = tmp_path / "tool"
    artifact.write_bytes(b"approved")
    digest = hashlib.sha256(b"approved").hexdigest()
    frozen = native_transport.freeze(artifact, tmp_path / "frozen" / "tool", digest)
    artifact.write_bytes(b"replaced original")
    observed = []

    def fake_s3(region, *args):
        uploaded = Path(args[args.index("--body") + 1]).read_bytes()
        observed.append(uploaded)
        import base64

        checksum = base64.b64encode(hashlib.sha256(uploaded).digest()).decode()
        assert args[args.index("--checksum-sha256") + 1] == checksum
        return {"VersionId": "immutable", "ChecksumSHA256": checksum}

    monkeypatch.setattr(native_transport, "aws", fake_s3)
    pointer = native_transport.upload(
        "us-east-1", "bucket", "key", frozen, expected_digest=digest
    )
    assert observed == [b"approved"]
    assert pointer["sha256"] == digest
    frozen.chmod(0o600)
    frozen.write_bytes(b"tampered staging")
    with pytest.raises(recipe.ImageRefused, match="approved digest"):
        native_transport.upload(
            "us-east-1", "bucket", "key", frozen, expected_digest=digest
        )
    assert len(observed) == 1


def test_native_dispatch_claim_survives_other_output_and_lost_reply(
    native_dispatch, native_transport, producer_plan, tmp_path, monkeypatch
):
    from types import SimpleNamespace

    args = SimpleNamespace(
        dispatch_id="once",
        project="native",
        account_id="111122223333",
        region="us-east-1",
        plan_sha256="a" * 64,
        deployment_sha256="b" * 64,
        output_bucket="native-output",
        output=str(tmp_path / "first"),
    )
    objects = {}
    lose_response = True

    def conditional_s3(region, *values):
        nonlocal lose_response
        assert values[values.index("--if-none-match") + 1] == "*"
        key = values[values.index("--key") + 1]
        if key in objects:
            raise RuntimeError("PreconditionFailed")
        objects[key] = Path(values[values.index("--body") + 1]).read_bytes()
        if lose_response:
            lose_response = False
            raise OSError("reply lost after durable write")
        return {
            "VersionId": "one",
            "ChecksumSHA256": values[values.index("--checksum-sha256") + 1],
        }

    monkeypatch.setattr(native_transport, "aws", conditional_s3)
    with pytest.raises(OSError, match="reply lost"):
        native_dispatch.claim_dispatch(args, producer_plan)
    args.output = str(tmp_path / "fresh-local-directory")
    with pytest.raises(RuntimeError, match="PreconditionFailed"):
        native_dispatch.claim_dispatch(args, producer_plan)
    assert len(objects) == 1
    claim = json.loads(next(iter(objects.values())))
    assert claim["approved_plan_sha256"] == args.plan_sha256
    assert claim["approved_deployment_sha256"] == args.deployment_sha256


@pytest.mark.parametrize("failure", ["plan-race", "lost-start-reply"])
def test_native_dispatch_refuses_race_and_never_restarts_claimed_id(
    native_dispatch,
    native_transport,
    producer_plan,
    approved_native_project,
    tmp_path,
    monkeypatch,
    failure,
):
    import hashlib
    from types import SimpleNamespace

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(runner.canonical(producer_plan))
    account, region = producer_plan["account_id"], producer_plan["region"]
    role = f"arn:aws:iam::{account}:role/dispatcher"
    project = approved_native_project
    project["environment"]["environmentVariables"] = [
        {"name": key, "value": value, "type": "PLAINTEXT"}
        for key, value in {
            "NATIVE_ACCOUNT_ID": account,
            "AWS_REGION": region,
            "NATIVE_INPUT_BUCKET": "native-input",
            "NATIVE_OUTPUT_BUCKET": "native-output",
            "NATIVE_DISPATCHER_ROLE_ARN": role,
        }.items()
    ]
    deployment = {
        "version": 1,
        "account_id": account,
        "region": region,
        "dispatcher_role_arn": role,
        "project_name": project["name"],
        "project": native_dispatch.project_view(project),
    }
    deployment_path = tmp_path / "deployment.json"
    deployment_path.write_text(json.dumps(deployment))
    args = SimpleNamespace(
        checkout=str(tmp_path / "checkout"),
        plan=str(plan_path),
        plan_sha256=hashlib.sha256(plan_path.read_bytes()).hexdigest(),
        deployment=str(deployment_path),
        deployment_sha256=hashlib.sha256(deployment_path.read_bytes()).hexdigest(),
        project=project["name"],
        dispatcher_role=role,
        account_id=account,
        region=region,
        bucket="native-input",
        output_bucket="native-output",
        dispatch_id="one-id",
        output=str(tmp_path / "first"),
    )
    claimed, starts = set(), []

    def fake_aws(region, *values):
        if values[0] == "sts":
            return {
                "Account": account,
                "Arn": f"arn:aws:sts::{account}:assumed-role/dispatcher/fixture",
            }
        if values[0] == "codebuild":
            return {"projects": [project]}
        key = values[values.index("--key") + 1]
        if key in claimed:
            raise RuntimeError("PreconditionFailed")
        claimed.add(key)
        return {
            "VersionId": "claim",
            "ChecksumSHA256": values[values.index("--checksum-sha256") + 1],
        }

    def prepare(values):
        output = Path(values.output)
        output.mkdir()
        (output / "dispatch.json").write_text(
            json.dumps(
                {
                    "envelope": {"sha256": "a" * 64},
                    "status": "START_PENDING",
                    "build_id": None,
                }
            )
        )
        if failure == "plan-race":
            changed = copy.deepcopy(producer_plan)
            changed["budget_approval_reference"] = "replaced-between-check-and-start"
            plan_path.write_text(runner.canonical(changed))

    def start(*args, **kwargs):
        assert kwargs["env"]["AWS_MAX_ATTEMPTS"] == "1"
        assert kwargs["env"]["AWS_RETRY_MODE"] == "standard"
        starts.append("shared-dispatch-invoked")
        raise OSError("lost start reply")

    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "9")
    monkeypatch.setenv("AWS_RETRY_MODE", "adaptive")
    monkeypatch.setattr(native_transport, "aws", fake_aws)
    monkeypatch.setattr(native_transport, "prepare", prepare)
    monkeypatch.setattr(
        native_transport, "upload", lambda *args, **kwargs: {"version": "receipt"}
    )
    monkeypatch.setattr(native_dispatch.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(native_dispatch.subprocess, "Popen", start)
    with pytest.raises((recipe.ImageRefused, OSError)):
        native_dispatch.dispatch(args)
    assert len(starts) == (0 if failure == "plan-race" else 1)
    if failure == "lost-start-reply":
        assert (
            json.loads((Path(args.output) / "child.json").read_text())["status"]
            == "UNKNOWN_REVIEW_REQUIRED"
        )
        args.output = str(tmp_path / "fresh-output")
        with pytest.raises(RuntimeError, match="PreconditionFailed"):
            native_dispatch.dispatch(args)
        assert len(starts) == 1
        assert not Path(args.output).exists()


def test_durable_native_receipts_survive_bulk_expiry_and_bind_original_plan(
    native_transport, producer_modules, producer_plan, tmp_path, monkeypatch
):
    import hashlib
    import shutil

    monkeypatch.setitem(sys.modules, "transport", native_transport)
    loaded = {}
    for name in ("retain", "reconcile"):
        module_spec = importlib.util.spec_from_file_location(
            "native_" + name, ROOT / "lane" / (name + ".py")
        )
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        loaded[name] = module
    work = tmp_path / "bulk"
    result = work / "result"
    result.mkdir(parents=True)
    raw = runner.canonical(producer_plan).encode()
    (result / "approved-plan.json").write_bytes(raw)
    start = {
        "build_id": "superplane-native-" + "a" * 32,
        "account_id": producer_plan["account_id"],
        "region": producer_plan["region"],
        "approved_plan_sha256": hashlib.sha256(raw).hexdigest(),
        "source_revision": producer_plan["source_revision"],
        "source_attestation_sha256": producer_plan["source_attestation_sha256"],
    }
    (result / "state.json").write_text(
        runner.canonical({**start, "phase": "failed", "cleanup": "unknown"})
    )
    (result / "cleanup-inventory.json").write_text(
        '{"images":[{"ImageId":"ami-retained"}]}'
    )
    objects = {"receipts/build/native-start.json": runner.canonical(start).encode()}

    def upload(region, bucket, key, path):
        objects[key] = Path(path).read_bytes()
        return {
            "key": key,
            "version": "original",
            "sha256": hashlib.sha256(objects[key]).hexdigest(),
        }

    monkeypatch.setattr(native_transport, "upload", upload)
    receipt = loaded["retain"].retain(
        work, "private-evidence", "build", producer_plan["region"], 1
    )
    assert receipt["cleanup"] == "unknown"
    assert all(key.startswith("receipts/") for key in objects)
    shutil.rmtree(work)  # Expiring bulk inputs/logs cannot remove these receipts.
    preserved_plan = json.loads(objects["receipts/build/approved-plan.json"])
    preserved_start = json.loads(objects["receipts/build/native-start.json"])
    calls = []

    def identity(*args):
        calls.append("identity")
        return {"Account": producer_plan["account_id"]}

    monkeypatch.setattr(loaded["reconcile"].build, "aws", identity)
    monkeypatch.setattr(
        producer_modules["producer"],
        "observe",
        lambda *args: {"images": [{"ImageId": "ami-retained"}]},
    )
    inventory = loaded["reconcile"].reconcile(preserved_plan, preserved_start)
    assert inventory["inventory"]["images"][0]["ImageId"] == "ami-retained"
    assert inventory["cleanup"] == "review_required"
    preserved_plan["budget_approval_reference"] = "different-plan"
    with pytest.raises(recipe.ImageRefused, match="original plan/source"):
        loaded["reconcile"].reconcile(preserved_plan, preserved_start)
    assert calls == ["identity"]


@pytest.fixture
def cni_v2_plan(producer_plan):
    plan = copy.deepcopy(producer_plan)
    plan["version"] = 2
    upstream = plan["upstream"]
    del upstream["cni_image"]
    del upstream["cni_files"]
    upstream["cni_sources"] = [
        {
            "image": "public.ecr.aws/fixture/daemon@sha256:" + "a" * 64,
            "files": {"/app/aws-cni": "aws-cni"},
        },
        {
            "image": "public.ecr.aws/fixture/init@sha256:" + "b" * 64,
            "files": {"/init/loopback": "loopback"},
        },
    ]
    return plan


@pytest.fixture
def fake_cni_docker(producer_modules, monkeypatch):
    from types import SimpleNamespace

    upstream = producer_modules["upstream"]
    state = {"calls": [], "created": {}, "missing": None}

    def create(argv, **kwargs):
        assert argv[:2] == ["docker", "create"]
        identity = str(len(state["created"]) + 1) * 64
        state["created"][identity] = argv[-1]
        state["calls"].append(argv)
        return SimpleNamespace(stdout=identity)

    def command(argv, **kwargs):
        state["calls"].append(argv)
        assert argv[1] in {"pull", "cp", "rm"}, "CNI containers must never start"
        if argv[1] == "cp":
            identity, source = argv[2].split(":", 1)
            if source != state["missing"]:
                Path(argv[-1]).write_bytes(
                    (state["created"][identity] + source).encode()
                )

    monkeypatch.setattr(upstream.subprocess, "run", create)
    monkeypatch.setattr(upstream, "command", command)
    return state


def test_two_cni_sources_union_and_provenance_pass_real_runtime_verifier(
    producer_modules, cni_v2_plan, fake_cni_docker, installed, tmp_path
):
    import shutil

    producer_modules["producer"].validate_plan(cni_v2_plan)
    before = runner.canonical(cni_v2_plan)
    stage = tmp_path / "assembled-cni"
    provenance = producer_modules["upstream"].assemble_cni(cni_v2_plan, stage)
    assert set(path.name for path in stage.iterdir()) == {"aws-cni", "loopback"}
    for expected, actual in zip(
        cni_v2_plan["upstream"]["cni_sources"], provenance, strict=True
    ):
        assert actual["image"] == expected["image"]
        for source, name in expected["files"].items():
            assert actual["files"][source] == {
                "destination": name,
                "sha256": recipe.file_sha(stage / name),
            }
            shutil.copyfile(stage / name, runner.CNI_ROOT / name)
    assert runner.canonical(cni_v2_plan) == before
    assert [call[2] for call in fake_cni_docker["calls"] if call[1] == "rm"] == [
        "1" * 64,
        "2" * 64,
    ]
    installed["closure"]["trees"][str(runner.CNI_ROOT)] = runner.tree_digest(
        runner.CNI_ROOT
    )
    raw, descriptor = recipe.manifest(installed)
    runner.MANIFEST_PATH.write_bytes(raw)
    contract = {
        "purpose": "node-bootstrap",
        "runtime_manifest": descriptor["runtime_manifest"],
        "wrapper_sha256": descriptor["bootstrap_wrapper_sha256"],
    }
    runner.verify_installation(
        contract, runner.RUNTIME_ROOT / "node_bootstrap_runner.py"
    )
    (runner.CNI_ROOT / "loopback").write_bytes(b"different-addon-binary")
    with pytest.raises(runner.RunnerRefused, match="closure differs"):
        runner.verify_installation(
            contract, runner.RUNTIME_ROOT / "node_bootstrap_runner.py"
        )


def test_missing_second_cni_image_file_cleans_each_created_container(
    producer_modules, cni_v2_plan, fake_cni_docker, tmp_path
):
    fake_cni_docker["missing"] = "/init/loopback"
    with pytest.raises(recipe.ImageRefused, match="regular executable"):
        producer_modules["upstream"].assemble_cni(cni_v2_plan, tmp_path / "cni")
    assert [call[2] for call in fake_cni_docker["calls"] if call[1] == "rm"] == [
        "1" * 64,
        "2" * 64,
    ]


@pytest.mark.parametrize(
    "bad", ["duplicate", "unpinned", "noncanonical", "source-count", "file-count"]
)
def test_invalid_cni_source_set_refuses_before_any_command(
    producer_modules, cni_v2_plan, fake_cni_docker, tmp_path, bad
):
    sources = cni_v2_plan["upstream"]["cni_sources"]
    if bad == "duplicate":
        sources[1]["files"] = {"/init/aws-cni": "aws-cni"}
    elif bad == "unpinned":
        sources[1]["image"] = "public.ecr.aws/fixture/init:latest"
    elif bad == "noncanonical":
        sources[1]["files"] = {"/init/./loopback": "loopback"}
    elif bad == "source-count":
        sources.append(copy.deepcopy(sources[0]))
    else:
        sources[1]["files"] = {
            f"/init/bin{index}": f"bin{index}" for index in range(65)
        }
    with pytest.raises(recipe.ImageRefused):
        producer_modules["upstream"].assemble_cni(cni_v2_plan, tmp_path / "cni")
    assert fake_cni_docker["calls"] == []
    assert not (tmp_path / "cni").exists()


def test_archived_v1_cni_plan_validation_and_bytes_are_unchanged(
    producer_modules, producer_plan
):
    # v1 admitted this path form; only the new v2 schema tightens canonical paths.
    producer_plan["upstream"]["cni_files"] = {"/app/./aws-cni": "aws-cni"}
    raw = runner.canonical(producer_plan)
    producer_modules["producer"].validate_plan(producer_plan)
    sources = producer_modules["upstream"].cni_sources(producer_plan)
    assert sources == [
        {
            "image": producer_plan["upstream"]["cni_image"],
            "files": {"/app/./aws-cni": "aws-cni"},
        }
    ]
    sources[0]["files"]["/app/new"] = "new"
    assert runner.canonical(producer_plan) == raw


def test_cni_plan_versions_do_not_silently_reinterpret_archived_fields(
    producer_modules, producer_plan, cni_v2_plan
):
    producer_plan["version"] = 2
    cni_v2_plan["version"] = 1
    for incompatible in (producer_plan, cni_v2_plan):
        with pytest.raises(recipe.ImageRefused):
            producer_modules["producer"].validate_plan(incompatible)
