"""Build a native GPU AMI from an unbooted published root and pinned upstreams."""

import argparse
import json
from pathlib import Path, PurePosixPath
import os
import shutil
import uuid

import build
import native_image as image
import upstream
import source_provenance

PLUGIN_VERSION = "1.8.2"
PLUGIN_SOURCE_COMMIT = "3896533621ce21e5d8277b7e86e9bf02b577045a"
API_DEVICE = "/dev/sdf"


def validate_plan(plan):
    image.exact(
        plan,
        {
            "version",
            "source_revision",
            "source_attestation_sha256",
            "source_files",
            "account_id",
            "region",
            "helper",
            "target",
            "builder",
            "tools",
            "upstream",
            "runtime",
            "extra_runtime_files",
            "build_timeout_minutes",
            "budget_approval_reference",
        },
    )
    if type(plan["version"]) is not int or plan["version"] != 1:
        raise image.ImageRefused("unsupported producer input")
    image.pattern(plan["source_revision"], r"[a-f0-9]{40}")
    image.digest(plan["source_attestation_sha256"])
    image.pattern(plan["account_id"], r"[0-9]{12}")
    image.pattern(plan["region"], r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]")
    image.exact(plan["source_files"], image.SOURCE_FILES)
    for digest in plan["source_files"].values():
        image.digest(digest)
    image.exact(plan["helper"], {"ami_id", "owner_id"})
    image.exact(
        plan["target"],
        {
            "ami_id",
            "owner_id",
            "snapshot_id",
            "root_device_name",
            "boot_mode",
            "ena_support",
        },
    )
    for value in (plan["helper"], plan["target"]):
        image.pattern(value["ami_id"], r"ami-[a-f0-9]{17}")
        image.pattern(value["owner_id"], r"[0-9]{12}")
    image.pattern(plan["target"]["snapshot_id"], r"snap-[a-f0-9]{17}")
    image.pattern(
        plan["target"]["root_device_name"], r"/dev/(?:sd|xvd)[a-z](?:[0-9]+)?"
    )
    if (
        plan["target"]["boot_mode"] not in {"legacy-bios", "uefi", "uefi-preferred"}
        or type(plan["target"]["ena_support"]) is not bool
    ):
        raise image.ImageRefused("explicit target boot/ENA properties required")
    image.exact(
        plan["builder"],
        {
            "instance_type",
            "subnet_id",
            "security_group_id",
            "instance_profile",
            "ssh_username",
            "kms_key_id",
            "target_volume_size_gb",
        },
    )
    for key, regex in {
        "instance_type": r"[a-z][a-z0-9-]*\.[a-z0-9]+",
        "subnet_id": r"subnet-[a-f0-9]{17}",
        "security_group_id": r"sg-[a-f0-9]{17}",
        "instance_profile": r"[A-Za-z0-9+=,.@_-]{1,128}",
        "ssh_username": r"[a-z_][a-z0-9_-]{0,31}",
        "kms_key_id": r"arn:aws(?:-us-gov)?:kms:[a-z0-9-]+:[0-9]{12}:key/[a-f0-9-]{36}",
    }.items():
        image.pattern(plan["builder"][key], regex)
    if plan["builder"]["kms_key_id"].split(":")[3:5] != [
        plan["region"],
        plan["account_id"],
    ]:
        raise image.ImageRefused("target volume KMS scope differs")
    if (
        type(plan["builder"]["target_volume_size_gb"]) is not int
        or not 20 <= plan["builder"]["target_volume_size_gb"] <= 500
    ):
        raise image.ImageRefused("explicit bounded target volume size required")
    image.exact(plan["tools"], {"packer", "amazon_plugin"})
    for value in plan["tools"].values():
        image.exact(value, {"version", "sha256"})
        image.pattern(value["version"], r"[0-9]+\.[0-9]+\.[0-9]+")
        image.digest(value["sha256"])
    if plan["tools"]["amazon_plugin"]["version"] != PLUGIN_VERSION:
        raise image.ImageRefused(
            "surrogate schema requires reviewed Amazon plugin 1.8.2"
        )
    image.exact(
        plan["upstream"],
        {"python", "nodeadm", "crictl", "go_image", "cni_image", "cni_files"},
    )
    for key in ("python", "nodeadm", "crictl"):
        value = plan["upstream"][key]
        image.exact(value, {"file", "sha256", "prefix"})
        image.pattern(value["file"], r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
        image.pattern(value["prefix"], r"[A-Za-z0-9._/-]{1,200}")
        if (
            value["prefix"].startswith("/")
            or ".." in PurePosixPath(value["prefix"]).parts
        ):
            raise image.ImageRefused("upstream prefix escapes input archive")
        image.digest(value["sha256"])
    if (
        plan["upstream"]["nodeadm"]["prefix"]
        != "amazon-eks-ami-" + image.runner.NODEADM_COMMIT
    ):
        raise image.ImageRefused(
            "nodeadm source must be the exact upstream commit archive"
        )
    for key in ("go_image", "cni_image"):
        image.pattern(
            plan["upstream"][key], r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[a-f0-9]{64}"
        )
    files = plan["upstream"]["cni_files"]
    if (
        not isinstance(files, dict)
        or not 1 <= len(files) <= 64
        or len(set(files.values())) != len(files)
    ):
        raise image.ImageRefused("explicit unique CNI image paths required")
    for original, target in files.items():
        image.pattern(original, r"/[A-Za-z0-9._/-]+")
        image.pattern(target, r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
        if ".." in PurePosixPath(original).parts:
            raise image.ImageRefused("CNI image path escapes")
    image.exact(plan["runtime"], image.runner.MANIFEST_FIELDS - {"artifact_sha256"})
    image.runner.validate_runtime_manifest(
        {
            **plan["runtime"],
            "artifact_sha256": image.sha(
                image.runner.canonical(plan["runtime"]).encode()
            ),
        }
    )
    extra = plan["extra_runtime_files"]
    if not isinstance(extra, list) or len(extra) > 128 or len(set(extra)) != len(extra):
        raise image.ImageRefused(
            "bounded explicit runtime/dlopen dependencies required"
        )
    for path in extra:
        image.pattern(path, r"/(?:usr|lib|lib64|bin|sbin|etc|opt)/[A-Za-z0-9_./+-]+")
        if ".." in PurePosixPath(path).parts:
            raise image.ImageRefused("runtime dependency path escapes")
    if (
        type(plan["build_timeout_minutes"]) is not int
        or not 10 <= plan["build_timeout_minutes"] <= 120
    ):
        raise image.ImageRefused("bounded build duration required")
    image.pattern(
        plan["budget_approval_reference"], r"[A-Za-z0-9][A-Za-z0-9:/._-]{0,199}"
    )
    return plan


def read_plan(path):
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise image.ImageRefused("producer plan exceeds bound")
    plan = image.runner.decode_json(raw)
    if raw.decode() != image.runner.canonical(plan):
        raise image.ImageRefused("canonical producer plan required")
    return validate_plan(plan)


def preflight(plan):
    if build.aws(plan, "sts", "get-caller-identity")["Account"] != plan["account_id"]:
        raise image.ImageRefused("producer account differs")
    found = {}
    for key in ("helper", "target"):
        images = build.aws(
            plan, "ec2", "describe-images", "--image-ids", plan[key]["ami_id"]
        )["Images"]
        if len(images) != 1 or any(
            images[0].get(field) != value
            for field, value in {
                "OwnerId": plan[key]["owner_id"],
                "State": "available",
                "Architecture": "x86_64",
                "RootDeviceType": "ebs",
                "VirtualizationType": "hvm",
            }.items()
        ):
            raise image.ImageRefused("published image provenance differs")
        found[key] = images[0]
    target = found["target"]
    roots = [
        mapping
        for mapping in target["BlockDeviceMappings"]
        if mapping["DeviceName"] == target["RootDeviceName"]
    ]
    if (
        len(roots) != 1
        or roots[0].get("Ebs", {}).get("SnapshotId") != plan["target"]["snapshot_id"]
        or target["RootDeviceName"] != plan["target"]["root_device_name"]
    ):
        raise image.ImageRefused("target AMI root snapshot differs")
    if (
        target.get("BootMode") != plan["target"]["boot_mode"]
        or target.get("EnaSupport") is not plan["target"]["ena_support"]
    ):
        raise image.ImageRefused("target boot/ENA facts differ")
    if found["helper"]["RootDeviceName"] == API_DEVICE:
        raise image.ImageRefused("surrogate mapping collides with helper root")
    snapshots = build.aws(
        plan,
        "ec2",
        "describe-snapshots",
        "--snapshot-ids",
        plan["target"]["snapshot_id"],
    )["Snapshots"]
    if (
        len(snapshots) != 1
        or snapshots[0].get("OwnerId") != plan["target"]["owner_id"]
        or snapshots[0].get("State") != "completed"
        or snapshots[0]["VolumeSize"] > plan["builder"]["target_volume_size_gb"]
    ):
        raise image.ImageRefused("target snapshot owner/state/size differs")
    found["snapshot"] = snapshots[0]
    return found


def template(plan, stage, output, build_id, caller=None):
    tags = {
        "superplane-native-build": build_id,
        "superplane-source": plan["source_revision"],
    }
    if caller is not None:
        image.pattern(caller, r"[A-Za-z0-9+=,.@_:-]{1,256}")
        tags["superplane-native-caller"] = caller
    source = {
        "region": plan["region"],
        "allowed_account_ids": [plan["account_id"]],
        "source_ami": plan["helper"]["ami_id"],
        "instance_type": plan["builder"]["instance_type"],
        "subnet_id": plan["builder"]["subnet_id"],
        "security_group_id": plan["builder"]["security_group_id"],
        "iam_instance_profile": plan["builder"]["instance_profile"],
        "ssh_username": plan["builder"]["ssh_username"],
        "ssh_interface": "private_ip",
        "communicator": "ssh",
        "associate_public_ip_address": False,
        "temporary_key_pair_name": build_id,
        "ami_name": build_id,
        "ami_description": "Superplane offline native root " + plan["source_revision"],
        "ami_virtualization_type": "hvm",
        "ami_architecture": "x86_64",
        "boot_mode": plan["target"]["boot_mode"],
        "ena_support": plan["target"]["ena_support"],
        "use_create_image": False,
        "metadata_options": {
            "http_endpoint": "enabled",
            "http_tokens": "required",
            "http_put_response_hop_limit": 1,
        },
        "launch_block_device_mappings": [
            {
                "device_name": API_DEVICE,
                "snapshot_id": plan["target"]["snapshot_id"],
                "encrypted": True,
                "kms_key_id": plan["builder"]["kms_key_id"],
                "volume_size": plan["builder"]["target_volume_size_gb"],
                "volume_type": "gp3",
                "delete_on_termination": True,
            }
        ],
        "ami_root_device": {
            "source_device_name": API_DEVICE,
            "device_name": plan["target"]["root_device_name"],
        },
        "run_tags": tags,
        "run_volume_tags": tags,
        # The dedicated lane cannot safely tag an untagged image. Pinned Packer
        # skips image CreateTags for this empty map; snapshot tags remain atomic.
        "tags": {} if caller is not None else tags,
        "snapshot_tags": tags,
        "force_deregister": False,
        "force_delete_snapshot": False,
    }
    root = "/tmp/superplane-native-producer"
    script = root + "/images/native-node/offline_root.py"
    return {
        "packer": {
            "required_plugins": {
                "amazon": {
                    "source": "github.com/hashicorp/amazon",
                    "version": "= " + PLUGIN_VERSION,
                }
            }
        },
        "source": {"amazon-ebssurrogate": {"native": source}},
        "build": [
            {
                "sources": ["source.amazon-ebssurrogate.native"],
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
                                "sudo "
                                + root
                                + "/python/bin/python3 -I -B -S "
                                + script
                                + " --plan "
                                + root
                                + "/plan.json --stage "
                                + root
                                + " --build-id "
                                + build_id
                            ]
                        }
                    },
                    {
                        "file": {
                            "direction": "download",
                            "source": root + "/evidence.json",
                            "destination": str(output / "root-evidence.download.json"),
                        }
                    },
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


def observe(plan, build_id):
    evidence = build.inventory(plan, build_id)
    by_name = build.aws(
        plan,
        "ec2",
        "describe-images",
        "--owners",
        "self",
        "--filters",
        json.dumps([{"Name": "name", "Values": [build_id]}]),
    )["Images"]
    evidence["images"] = list(
        {value["ImageId"]: value for value in [*evidence["images"], *by_name]}.values()
    )
    return evidence


def record(plan, output, recipe, base, build_id, evidence):
    build.require_cleanup(evidence)
    if len(evidence["images"]) != 1:
        raise image.ImageRefused("exact uniquely named producer AMI unavailable")
    result = evidence["images"][0]
    for key, expected in {
        "Name": build_id,
        "OwnerId": plan["account_id"],
        "State": "available",
        "Public": False,
        "Architecture": "x86_64",
        "VirtualizationType": "hvm",
        "RootDeviceName": plan["target"]["root_device_name"],
        "BootMode": plan["target"]["boot_mode"],
        "EnaSupport": plan["target"]["ena_support"],
    }.items():
        if result.get(key) != expected:
            raise image.ImageRefused("producer AMI identity/properties differ")
    mappings = result["BlockDeviceMappings"]
    if (
        len(mappings) != 1
        or mappings[0]["DeviceName"] != result["RootDeviceName"]
        or mappings[0].get("Ebs", {}).get("DeleteOnTermination") is not True
    ):
        raise image.ImageRefused(
            "AMI contains helper/extra mappings or retains runtime volume"
        )
    build.require_snapshot_provenance(plan, result, evidence["snapshots"])
    root = image.runner.decode_json(
        (output / "root-evidence.download.json").read_bytes()
    )
    from offline_root import bound_volume

    bound_volume(plan, build_id, root["helper_instance_id"], [root["target_volume"]])
    if evidence["snapshots"][0].get("VolumeId") != root["target_volume"]["VolumeId"]:
        raise image.ImageRefused(
            "final snapshot did not come from the observed target volume"
        )
    raw = image.runner.canonical(root["manifest"]).encode()
    descriptor = root["descriptor"]
    if root["manifest"]["runtime"] != plan["runtime"] or descriptor[
        "runtime_manifest"
    ] != {**plan["runtime"], "artifact_sha256": image.sha(raw)}:
        raise image.ImageRefused(
            "generated runtime descriptor differs from completed root"
        )
    image.runner.validate_runtime_manifest(descriptor["runtime_manifest"])
    for name, field in (
        ("node_bootstrap_runner.py", "bootstrap_wrapper_sha256"),
        ("node_probe_runner.py", "probe_wrapper_sha256"),
    ):
        if (
            descriptor[field]
            != plan["source_files"]["executor/superplane_executor/" + name]
        ):
            raise image.ImageRefused("generated wrapper source provenance differs")
    build.write(output / "root-evidence.json", root)
    build.write(output / "descriptor.json", descriptor)
    build.write(
        output / "result.json",
        {
            "version": 1,
            "build_id": build_id,
            "source_revision": plan["source_revision"],
            "base": base,
            "image": result,
            "retained_snapshots": evidence["snapshots"],
            "root_evidence_sha256": image.sha(image.runner.canonical(root).encode()),
            "plan_sha256": image.sha(image.runner.canonical(plan).encode()),
            "plugin_source_commit": PLUGIN_SOURCE_COMMIT,
            "descriptor": descriptor,
            "temporary_resource_audit_passed": True,
            "promotion": "review_required",
            "live_gpu_acceptance": "pending",
        },
    )


def run(args):
    plan = read_plan(args.plan)
    image.verify_sources(plan)
    repo = image.APP_ROOT.parents[2]
    provenance = source_provenance.verify(
        repo,
        args.source_attestation,
        plan["source_attestation_sha256"],
        plan["source_revision"],
    )
    output = Path(args.output).resolve()
    if output.is_relative_to(repo):
        raise image.ImageRefused("output must be outside source checkout")
    packer = build.verify_tool(args.packer, plan["tools"]["packer"])
    plugin = build.verify_tool(args.amazon_plugin, plan["tools"]["amazon_plugin"])
    if (
        build.run([packer, "version"]).splitlines()[0]
        != "Packer v" + plan["tools"]["packer"]["version"]
        or json.loads(build.run([plugin, "describe"]))["version"] != PLUGIN_VERSION
    ):
        raise image.ImageRefused("pinned build tool versions differ")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    build.write(output / "source-provenance.json", provenance)
    build_id = "superplane-native-" + uuid.uuid4().hex
    state = {
        "build_id": build_id,
        "account_id": plan["account_id"],
        "region": plan["region"],
        "phase": "preflight",
        "cleanup": "not_started",
        "approved_plan_sha256": image.sha(image.runner.canonical(plan).encode()),
        "source_revision": plan["source_revision"],
        "source_attestation_sha256": plan["source_attestation_sha256"],
        "source_tree": provenance["tree"],
    }
    build.write(output / "state.json", state)
    if os.environ.get("SUPERPLANE_NATIVE_LANE") == "caller-bound-no-ami-tags":
        receipt = os.environ.get("NATIVE_STATE_RECEIPT_URI", "")
        image.pattern(
            receipt,
            r"s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/receipts/[A-Za-z0-9-]+/native-start.json",
        )
        # A killed worker may never upload final evidence. Publish the original
        # native identity before even preflight, and refuse launch on lost upload.
        build.write(output / "approved-plan.json", plan)
        for source, name in (
            (output / "approved-plan.json", "approved-plan.json"),
            (output / "source-provenance.json", "source-provenance.json"),
            (output / "state.json", "native-start.json"),
        ):
            build.run(
                [
                    "aws",
                    "s3",
                    "cp",
                    str(source),
                    receipt.rsplit("/", 1)[0] + "/" + name,
                    "--region",
                    plan["region"],
                ]
            )
    try:
        base = preflight(plan)
        build.write(output / "base-provenance.json", base)
        stage = output / "source"
        build.write(
            output / "upstream-provenance.json",
            upstream.assemble(plan, Path(args.inputs), stage),
        )
        for name in (
            *image.SOURCE_FILES,
            "executor/superplane_executor/__init__.py",
            *[
                "images/native-node/" + name
                for name in (
                    "native_image.py",
                    "build.py",
                    "producer.py",
                    "upstream.py",
                    "offline_root.py",
                    "source_provenance.py",
                )
            ],
        ):
            destination = stage / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(image.APP_ROOT / name, destination)
        build.write(stage / "plan.json", plan)
        env = {
            **os.environ,
            "PACKER_PLUGIN_PATH": str(output / "plugins"),
            "PACKER_LOG": "1",
            "PACKER_LOG_PATH": str(output / "packer.log"),
        }
        build.run(
            [
                packer,
                "plugins",
                "install",
                "--path",
                plugin,
                "github.com/hashicorp/amazon",
            ],
            env=env,
        )
        recipe = output / "native.pkr.json"
        caller = None
        if os.environ.get("SUPERPLANE_NATIVE_LANE") == "caller-bound-no-ami-tags":
            caller = build.aws(plan, "sts", "get-caller-identity")["UserId"]
        build.write(recipe, template(plan, stage, output, build_id, caller=caller))
        build.run([packer, "validate", str(recipe)], env=env)
        build.complete_build(
            plan,
            output,
            recipe,
            base,
            build_id,
            packer,
            env,
            state,
            observe=observe,
            record=record,
        )
    except BaseException as error:
        state.update(phase="failed", error_kind=type(error).__name__)
        build.write(output / "state.json", state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "plan",
        "source-attestation",
        "inputs",
        "packer",
        "amazon-plugin",
        "output",
    ):
        parser.add_argument("--" + name, required=True)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
