"""Explicit-account native AMI build using supplied, digest-verified Packer tools."""

import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import uuid

import native_image as image


def run(arguments, *, env=None):
    return subprocess.run(
        arguments, check=True, capture_output=True, text=True, env=env, timeout=120
    ).stdout


def aws(lock, *arguments):
    return json.loads(
        run(
            [
                "aws",
                *arguments,
                "--region",
                lock["region"],
                "--output",
                "json",
                "--no-cli-pager",
            ]
        )
    )


def write(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix="." + path.name + ".", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(image.runner.canonical(value).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def verify_tool(path, expected):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or image.file_sha(path) != expected["sha256"]:
        raise image.ImageRefused("build tool digest differs")
    return str(path)


def template(lock, stage, output, build_id):
    tags = {
        "superplane-native-build": build_id,
        "superplane-source": lock["source_revision"],
    }
    builder = lock["builder"]
    source = {
        "region": lock["region"],
        "allowed_account_ids": [lock["account_id"]],
        "source_ami": lock["base"]["ami_id"],
        "instance_type": builder["instance_type"],
        "subnet_id": builder["subnet_id"],
        "security_group_id": builder["security_group_id"],
        "iam_instance_profile": builder["instance_profile"],
        "ssh_username": builder["ssh_username"],
        "communicator": "ssh",
        "ssh_timeout": "5m",
        "ssh_interface": "private_ip",
        "associate_public_ip_address": False,
        "temporary_key_pair_name": build_id,
        "ami_name": build_id,
        "ami_description": "Superplane prepared native node " + lock["source_revision"],
        "encrypt_boot": True,
        "kms_key_id": builder["kms_key_id"],
        "metadata_options": {
            "http_endpoint": "enabled",
            "http_tokens": "required",
            "http_put_response_hop_limit": 1,
        },
        "run_tags": tags,
        "run_volume_tags": tags,
        "tags": tags,
        "snapshot_tags": tags,
        "force_deregister": False,
        "force_delete_snapshot": False,
    }
    root = "/tmp/superplane-native-build"
    program = root + "/images/native-node/native_image.py"
    return {
        "packer": {
            "required_plugins": {
                "amazon": {
                    "version": "= " + lock["tools"]["amazon_plugin"]["version"],
                    "source": "github.com/hashicorp/amazon",
                }
            }
        },
        "source": {"amazon-ebs": {"native": source}},
        "build": [
            {
                "sources": ["source.amazon-ebs.native"],
                "provisioner": [
                    {
                        "shell": {
                            "inline": ["test ! -e " + root, "mkdir -m 700 " + root]
                        }
                    },
                    {"file": {"source": str(stage) + "/", "destination": root}},
                    {
                        "shell": {
                            "inline": [
                                "sudo /usr/bin/python3.12 "
                                + program
                                + " prepare --lock "
                                + root
                                + "/input-lock.json --bundle "
                                + root
                                + "/runtime.tar",
                                "sudo /usr/bin/python3.12 "
                                + program
                                + " verify --lock "
                                + root
                                + "/input-lock.json",
                            ]
                        }
                    },
                    {
                        "file": {
                            "direction": "download",
                            "source": "/opt/superplane/image-evidence/descriptor.json",
                            "destination": str(output / "descriptor.json"),
                        }
                    },
                    {
                        "file": {
                            "direction": "download",
                            "source": "/opt/superplane/node-runtime/manifest.json",
                            "destination": str(output / "runtime-manifest.json"),
                        }
                    },
                    # Only this recipe's isolated upload directory is removed. No
                    # enrolled worker state is erased or reused to complete a build.
                    {"shell": {"inline": ["sudo rm -rf -- " + root]}},
                ],
                "post-processor": [
                    {
                        "manifest": {
                            "output": str(output / "packer-manifest.json"),
                            "strip_path": True,
                        }
                    }
                ],
            }
        ],
    }


def check_target(lock):
    if aws(lock, "sts", "get-caller-identity")["Account"] != lock["account_id"]:
        raise image.ImageRefused("build account differs from reviewed lock")
    images = aws(lock, "ec2", "describe-images", "--image-ids", lock["base"]["ami_id"])[
        "Images"
    ]
    if len(images) != 1:
        raise image.ImageRefused("base image unavailable")
    base = images[0]
    if any(
        base.get(key) != value
        for key, value in {
            "OwnerId": lock["base"]["owner_id"],
            "Architecture": "x86_64",
            "State": "available",
            "RootDeviceType": "ebs",
            "VirtualizationType": "hvm",
        }.items()
    ):
        raise image.ImageRefused("base image provenance differs")
    return base


def inventory(lock, build_id):
    filters = json.dumps(
        [{"Name": "tag:superplane-native-build", "Values": [build_id]}]
    )
    return {
        "instances": aws(lock, "ec2", "describe-instances", "--filters", filters)[
            "Reservations"
        ],
        "volumes": aws(lock, "ec2", "describe-volumes", "--filters", filters)[
            "Volumes"
        ],
        "images": aws(
            lock, "ec2", "describe-images", "--owners", "self", "--filters", filters
        )["Images"],
        "snapshots": aws(
            lock,
            "ec2",
            "describe-snapshots",
            "--owner-ids",
            "self",
            "--filters",
            filters,
        )["Snapshots"],
        "key_pairs": aws(
            lock,
            "ec2",
            "describe-key-pairs",
            "--filters",
            json.dumps([{"Name": "key-name", "Values": [build_id]}]),
        )["KeyPairs"],
    }


def require_cleanup(evidence):
    instances = [
        instance for group in evidence["instances"] for instance in group["Instances"]
    ]
    if (
        any(instance["State"]["Name"] != "terminated" for instance in instances)
        or evidence["volumes"]
        or evidence["key_pairs"]
    ):
        raise image.ImageRefused(
            "temporary builder resources remain; preserve cleanup inventory"
        )


def require_snapshot_provenance(lock, result, observed):
    snapshots = {snapshot["SnapshotId"]: snapshot for snapshot in observed}
    required = {
        mapping["Ebs"]["SnapshotId"]
        for mapping in result["BlockDeviceMappings"]
        if "Ebs" in mapping
    }
    if not required or required != snapshots.keys() or len(snapshots) != len(observed):
        raise image.ImageRefused(
            "missing or extra build snapshots keep cleanup unresolved"
        )
    if any(
        snapshot.get("OwnerId") != lock["account_id"]
        or snapshot.get("Encrypted") is not True
        or snapshot.get("KmsKeyId") != lock["builder"]["kms_key_id"]
        for snapshot in snapshots.values()
    ):
        raise image.ImageRefused("built snapshot provenance or encryption incomplete")


def build(args):
    lock = image.read_lock(args.lock)
    image.verify_sources(lock)
    root = image.APP_ROOT.parents[2]
    if run(["git", "-C", str(root), "rev-parse", "HEAD"]).strip() != lock[
        "source_revision"
    ] or run(["git", "-C", str(root), "status", "--porcelain"]):
        raise image.ImageRefused("clean exact reviewed source revision required")
    for path, expected in (
        (args.bundle, lock["bundle_sha256"]),
        (args.dependency_review, lock["dependency_review_sha256"]),
    ):
        if image.file_sha(path) != expected:
            raise image.ImageRefused("reviewed image input differs")
    packer = verify_tool(args.packer, lock["tools"]["packer"])
    plugin = verify_tool(args.amazon_plugin, lock["tools"]["amazon_plugin"])
    if (
        run([packer, "version"]).splitlines()[0]
        != "Packer v" + lock["tools"]["packer"]["version"]
    ):
        raise image.ImageRefused("Packer version differs")
    if (
        json.loads(run([plugin, "describe"]))["version"]
        != lock["tools"]["amazon_plugin"]["version"]
    ):
        raise image.ImageRefused("Amazon plugin version differs")
    output = Path(args.output).resolve()
    if output.is_relative_to(root):
        raise image.ImageRefused("build output must be outside the source checkout")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    build_id = "superplane-native-" + uuid.uuid4().hex
    state = {
        "build_id": build_id,
        "account_id": lock["account_id"],
        "region": lock["region"],
        "source_revision": lock["source_revision"],
        "input_lock_sha256": image.sha(image.runner.canonical(lock).encode()),
        "phase": "preflight",
        "cleanup": "not_started",
    }
    write(output / "state.json", state)
    try:
        prepare_build(args, lock, packer, plugin, output, build_id, state)
    except BaseException as error:
        state.update(phase="failed", error_kind=type(error).__name__)
        write(output / "state.json", state)
        raise


def prepare_build(args, lock, packer, plugin, output, build_id, state):
    base = check_target(lock)
    write(output / "base-image.json", base)
    stage = output / "source"
    stage.mkdir()
    for name in (
        *image.SOURCE_FILES,
        "executor/superplane_executor/__init__.py",
        "images/native-node/native_image.py",
    ):
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(image.APP_ROOT / name, target)
    shutil.copyfile(args.lock, stage / "input-lock.json")
    shutil.copyfile(args.bundle, stage / "runtime.tar")
    shutil.copyfile(args.dependency_review, output / "dependency-review")
    env = {
        **os.environ,
        "PACKER_PLUGIN_PATH": str(output / "plugins"),
        "PACKER_LOG": "1",
        "PACKER_LOG_PATH": str(output / "packer.log"),
    }
    run(
        [packer, "plugins", "install", "--path", plugin, "github.com/hashicorp/amazon"],
        env=env,
    )
    recipe = output / "native.pkr.json"
    write(recipe, template(lock, stage, output, build_id))
    run([packer, "validate", str(recipe)], env=env)
    complete_build(lock, output, recipe, base, build_id, packer, env, state)


def execute_packer(lock, output, recipe, packer, env):
    # Packer owns its temporary instance, volumes and ephemeral key. Keep cleanup
    # enabled on provisioner failure; interruption requests its normal cleanup.
    with (output / "build.log").open("w") as log:
        process = subprocess.Popen(
            [packer, "build", "-on-error=cleanup", str(recipe)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
        try:
            status = process.wait(timeout=lock["build_timeout_minutes"] * 60)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise image.ImageRefused(
                "build interrupted; inspect durable cleanup inventory"
            ) from None
        if status:
            raise image.ImageRefused(
                "Packer failed; inspect build and cleanup evidence"
            )


def record_result(lock, output, recipe, base, build_id, evidence):
    require_cleanup(evidence)
    if len(evidence["images"]) != 1:
        raise image.ImageRefused("exact built image missing")
    result = evidence["images"][0]
    if (
        result.get("State") != "available"
        or result.get("OwnerId") != lock["account_id"]
        or result.get("Architecture") != "x86_64"
        or result.get("Public") is not False
    ):
        raise image.ImageRefused("built image not available in approved account")
    require_snapshot_provenance(lock, result, evidence["snapshots"])
    descriptor = json.loads((output / "descriptor.json").read_text())
    image.runner.validate_runtime_manifest(descriptor["runtime_manifest"])
    expected_manifest = image.runner.canonical(
        {"version": 1, "runtime": lock["runtime"], **lock["closure"]}
    ).encode()
    expected_descriptor = {
        "version": 1,
        "runtime_manifest": {
            **lock["runtime"],
            "artifact_sha256": image.sha(expected_manifest),
        },
        "bootstrap_wrapper_sha256": lock["source_files"][
            "executor/superplane_executor/node_bootstrap_runner.py"
        ],
        "probe_wrapper_sha256": lock["source_files"][
            "executor/superplane_executor/node_probe_runner.py"
        ],
    }
    if (
        output / "runtime-manifest.json"
    ).read_bytes() != expected_manifest or descriptor != expected_descriptor:
        raise image.ImageRefused("returned image artifacts differ from reviewed inputs")
    write(
        output / "result.json",
        {
            "version": 1,
            "build_id": build_id,
            "source_revision": lock["source_revision"],
            "account_id": lock["account_id"],
            "region": lock["region"],
            "image_id": result["ImageId"],
            "base_image": base,
            "built_image": result,
            "retained_snapshots": evidence["snapshots"],
            "input_lock_sha256": image.sha(image.runner.canonical(lock).encode()),
            "dependency_review_sha256": lock["dependency_review_sha256"],
            "descriptor": descriptor,
            "recipe_sha256": image.sha(recipe.read_bytes()),
            "temporary_resource_audit_passed": True,
            "live_gpu_acceptance": "pending",
        },
    )


def complete_build(
    lock,
    output,
    recipe,
    base,
    build_id,
    packer,
    env,
    state,
    *,
    observe=None,
    record=None,
):
    observe = observe or inventory
    record = record or record_result
    state.update(phase="building", cleanup="unknown", build_outcome="unknown")
    write(output / "state.json", state)
    try:
        try:
            execute_packer(lock, output, recipe, packer, env)
            state["build_outcome"] = "succeeded"
        except BaseException as error:
            state.update(build_outcome="failed", build_error_kind=type(error).__name__)
            raise
        finally:
            state["phase"] = "reconciling_cleanup"
            write(output / "state.json", state)
            # An unreadable inventory leaves the durable state explicitly unknown.
            evidence = observe(lock, build_id)
            write(output / "cleanup-inventory.json", evidence)
            state["cleanup"] = "inventory_recorded"
            write(output / "state.json", state)
        record(lock, output, recipe, base, build_id, evidence)
        state.update(phase="complete", cleanup="audited")
        write(output / "state.json", state)
    except BaseException as error:
        state.update(phase="failed", error_kind=type(error).__name__)
        write(output / "state.json", state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "lock",
        "bundle",
        "dependency-review",
        "packer",
        "amazon-plugin",
        "output",
    ):
        parser.add_argument("--" + name, required=True)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
