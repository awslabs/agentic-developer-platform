"""Prepare only an authenticated, never-booted target EBS root on a build helper."""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import native_image as image  # noqa: E402
import build  # noqa: E402

MOUNT = Path("/mnt/superplane-native-target")
DEVICE = "/dev/sdf"


def command(arguments, *, env=None):
    return subprocess.run(
        arguments, check=True, capture_output=True, text=True, timeout=120, env=env
    ).stdout


def aws(plan, *arguments):
    return json.loads(
        command(
            [
                "aws",
                *arguments,
                "--region",
                plan["region"],
                "--output",
                "json",
                "--no-cli-pager",
            ]
        )
    )


def helper_identity():
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), image.runner._NoRedirect()
    )
    with opener.open(
        urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
        ),
        timeout=5,
    ) as response:
        token = response.read(4096).decode()
    with opener.open(
        urllib.request.Request(
            "http://169.254.169.254/latest/meta-data/instance-id",
            headers={"X-aws-ec2-metadata-token": token},
        ),
        timeout=5,
    ) as response:
        instance = response.read(100).decode()
    image.pattern(instance, r"i-[a-f0-9]{17}")
    return instance


def bound_volume(plan, build_id, instance, volumes):
    if len(volumes) != 1:
        raise image.ImageRefused("exact target attachment is unavailable")
    volume = volumes[0]
    attachments = volume.get("Attachments", [])
    tags = {tag["Key"]: tag["Value"] for tag in volume.get("Tags", [])}
    if (
        volume.get("SnapshotId") != plan["target"]["snapshot_id"]
        or volume.get("Encrypted") is not True
        or volume.get("KmsKeyId") != plan["builder"]["kms_key_id"]
        or volume.get("State") != "in-use"
        or tags.get("superplane-native-build") != build_id
        or tags.get("superplane-source") != plan["source_revision"]
        or len(attachments) != 1
        or any(
            attachments[0].get(key) != value
            for key, value in {
                "InstanceId": instance,
                "Device": DEVICE,
                "State": "attached",
                "DeleteOnTermination": True,
            }.items()
        )
    ):
        raise image.ImageRefused(
            "target volume differs from original snapshot/build attachment"
        )
    image.pattern(volume["VolumeId"], r"vol-[a-f0-9]{17}")
    return volume


def device_for(volume_id, blockdevices):
    expected = volume_id.replace("-", "")
    matches = [
        device
        for device in blockdevices
        if str(device.get("serial", "")).replace("-", "").strip() == expected
    ]
    if len(matches) != 1 or matches[0].get("type") != "disk":
        raise image.ImageRefused("EBS volume cannot be bound to one Linux block device")
    device = matches[0]
    choices = [device, *device.get("children", [])]
    for item in choices:
        if item.get("mountpoints") and any(item["mountpoints"]):
            raise image.ImageRefused("target disk is already mounted")
        if item.get("type") not in {"disk", "part"}:
            raise image.ImageRefused("unsupported target block layout")
        image.pattern(item["name"], r"/dev/[a-zA-Z0-9]+")
    return [item for item in choices if item.get("fstype") in {"xfs", "ext4"}]


def target_path(root, absolute):
    """Resolve absolute guest links inside the guest, never against helper /etc."""
    if not absolute.startswith("/") or ".." in PurePosixPath(absolute).parts:
        raise image.ImageRefused("canonical target path required")
    parts = list(PurePosixPath(absolute).parts[1:])
    resolved = []
    links = 0
    while parts:
        component = parts.pop(0)
        current = root.joinpath(*resolved, component)
        if current.is_symlink():
            links += 1
            if links > 40:
                raise image.ImageRefused("target alias cycle")
            link = os.readlink(current)
            replacement = posixpath.normpath(
                link if link.startswith("/") else "/" + "/".join([*resolved, link])
            )
            parts = list(PurePosixPath(replacement).parts[1:]) + parts
            resolved = []
        else:
            resolved.append(component)
    result = root.joinpath(*resolved)
    if not result.is_relative_to(root):
        raise image.ImageRefused("target path escaped its mounted root")
    return result


def refuse_enrollment(root):
    for absolute in image.ENROLLMENT_PATHS:
        path = target_path(root, absolute)
        if path.exists() or path.is_symlink():
            raise image.ImageRefused(
                "source snapshot contains enrollment state; never erase it"
            )
    for absolute in ("/etc/eks/nodeadm.d", "/var/lib/amazon/ssm"):
        path = target_path(root, absolute)
        if path.exists() and any(
            absolute.endswith("nodeadm.d") or entry.name.startswith(("i-", "mi-"))
            for entry in path.iterdir()
        ):
            raise image.ImageRefused(
                "source snapshot has ambient config or SSM identity"
            )


def secure_entry(path):
    info = path.lstat()
    if (
        info.st_uid != 0
        or info.st_mode & 0o022
        or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
    ):
        raise image.ImageRefused(
            "target artifact is not an immutable root-owned file/directory"
        )
    return info


def ensure_directory(root, absolute):
    current = root
    for part in PurePosixPath(absolute).parts[1:]:
        current = current / part
        if current.is_symlink():
            raise image.ImageRefused(
                "installation parent is an unresolved target alias"
            )
        if not current.exists():
            current.mkdir(mode=0o755)
        if not stat.S_ISDIR(secure_entry(current).st_mode):
            raise image.ImageRefused("installation parent is not a directory")
    return current


def copy_resolved(root, absolute, destination, ancestry=()):
    source = target_path(root, absolute)
    info = secure_entry(source)
    if source in ancestry:
        raise image.ImageRefused("directory alias cycle in target artifacts")
    if stat.S_ISDIR(info.st_mode):
        destination.mkdir(mode=0o755)
        for child in source.iterdir():
            copy_resolved(
                root,
                "/" + str(child.relative_to(root)),
                destination / child.name,
                (*ancestry, source),
            )
    else:
        with source.open("rb") as incoming, destination.open("xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        destination.chmod(0o755 if info.st_mode & 0o111 else 0o644)


def materialize(root, absolute, aliases):
    current = root
    for component in PurePosixPath(absolute).parts[1:]:
        current = current / component
        if current.is_symlink():
            original = "/" + str(current.relative_to(root))
            resolved = target_path(root, original)
            temporary = current.with_name(current.name + ".superplane-materialized")
            copy_resolved(root, original, temporary)
            aliases.append(
                {"path": original, "resolved": "/" + str(resolved.relative_to(root))}
            )
            current.unlink()  # only the copied artifact alias, never enrollment state
            os.replace(temporary, current)
        secure_entry(current)
    return current


def install_tree(source, root, absolute):
    destination = ensure_directory(root, absolute)
    if destination.exists() and (destination.is_symlink() or not destination.is_dir()):
        raise image.ImageRefused("target tree has incompatible type")
    destination.mkdir(parents=True, exist_ok=True, mode=0o755)
    for path in source.rglob("*"):
        if path.is_symlink():
            raise image.ImageRefused("generated input contains an unresolved link")
        target = destination / path.relative_to(source)
        if path.is_dir():
            ensure_directory(root, "/" + str(target.relative_to(root)))
        elif path.is_file():
            ensure_directory(root, "/" + str(target.parent.relative_to(root)))
            if target.is_symlink():
                raise image.ImageRefused("target overlay would follow an alias")
            if target.exists() and not stat.S_ISREG(secure_entry(target).st_mode):
                raise image.ImageRefused("target overlay would replace a special file")
            with path.open("rb") as incoming, target.open("wb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            target.chmod(0o755 if path.stat().st_mode & 0o111 else 0o644)
        else:
            raise image.ImageRefused("generated input contains a special file")


def mask_target(root):
    directory = root / "etc/systemd/system"
    if target_path(root, "/etc/systemd/system") != directory:
        raise image.ImageRefused("target systemd directory aliases another location")
    secure_entry(directory)
    for name in ("nodeadm-config.service", "nodeadm-run.service"):
        path = directory / name
        if path.is_dir() and not path.is_symlink():
            raise image.ImageRefused("target unit path is a directory")
        if path.exists() or path.is_symlink():
            path.unlink()
        path.symlink_to("/dev/null")


def tree_hash(root, excluded=None):
    values = {}
    secure_entry(root)
    for path in root.rglob("*"):
        secure_entry(path)
        if path.is_file() and path != excluded:
            values[str(path.relative_to(root))] = image.file_sha(path)
    if not values:
        raise image.ImageRefused("empty generated runtime tree")
    return image.sha(image.runner.canonical(values).encode())


def elf64(path):
    with path.open("rb") as source:
        header = source.read(20)
    return header.startswith(b"\x7fELF\x02\x01") and header[18:20] == b"\x3e\x00"


def closure(root, extra_files, aliases):
    seeds = (
        set(image.runner.REQUIRED_FILES)
        | {"/bin/sh", "/usr/bin/env"}
        | set(extra_files)
    )
    trees = ("/opt/superplane/node-runtime", "/opt/cni/bin")
    for tree in trees:
        for path in (root / tree[1:]).rglob("*"):
            if path.is_file():
                with path.open("rb") as source:
                    if source.read(4) == b"\x7fELF":
                        seeds.add("/" + str(path.relative_to(root)))
    cache = command(["ldconfig", "-r", str(root), "-p"])
    libraries = {}
    for line in cache.splitlines():
        match = re.match(r"\s*(\S+)\s+\([^)]*\)\s+=>\s+(/\S+)\s*$", line)
        if match:
            libraries.setdefault(match[1], []).append(match[2])
    visited, files = set(), {}
    while seeds:
        absolute = seeds.pop()
        if absolute in visited:
            continue
        visited.add(absolute)
        if len(visited) > 10000:
            raise image.ImageRefused("ELF closure exceeds review bound")
        path = materialize(root, absolute, aliases)
        if not any(PurePosixPath(absolute).is_relative_to(tree) for tree in trees):
            files[absolute] = image.file_sha(path)
        with path.open("rb") as source:
            header = source.read(4096)
        if header.startswith(b"#!"):
            interpreter = header.splitlines()[0][2:].strip().split()[0].decode()
            if not interpreter.startswith("/"):
                raise image.ImageRefused("absolute script interpreter required")
            seeds.add(interpreter)
        if not header.startswith(b"\x7fELF"):
            continue
        if not elf64(path):
            raise image.ImageRefused("target executable is not Linux x86_64 ELF")
        metadata = command(["readelf", "--program-headers", "--dynamic", str(path)])
        for interpreter in re.findall(
            r"Requesting program interpreter:\s*([^\]]+)\]", metadata
        ):
            seeds.add(interpreter.strip())
        search = []
        for value in re.findall(r"\((?:RUNPATH|RPATH)\).*?\[([^\]]*)\]", metadata):
            for location in value.split(":"):
                location = location.replace(
                    "${ORIGIN}", posixpath.dirname(absolute)
                ).replace("$ORIGIN", posixpath.dirname(absolute))
                if not location.startswith("/") or "$" in location:
                    raise image.ImageRefused("unresolved ELF loader search path")
                search.append(posixpath.normpath(location))
        for library in re.findall(r"\(NEEDED\).*?\[([^\]]+)\]", metadata):
            candidates = [directory + "/" + library for directory in search]
            candidates += libraries.get(library, [])
            candidates += [
                directory + "/" + library
                for directory in ("/lib64", "/usr/lib64", "/lib", "/usr/lib")
            ]
            selected = next(
                (
                    candidate
                    for candidate in candidates
                    if target_path(root, candidate).is_file()
                    and elf64(target_path(root, candidate))
                ),
                None,
            )
            if selected is None:
                raise image.ImageRefused("unresolved ELF dependency: " + library)
            seeds.add(selected)
    if len(files) > 256:
        raise image.ImageRefused(
            "generated external file closure exceeds runtime manifest"
        )
    return {
        "files": files,
        "trees": {
            tree: tree_hash(
                root / tree[1:], root / "opt/superplane/node-runtime/manifest.json"
            )
            for tree in trees
        },
    }


def prepare_target(plan, stage, root, evidence):
    refuse_enrollment(root)
    aliases = []
    for absolute in ("/opt", "/usr/bin", "/etc/eks", "/var/lib"):
        materialize(root, absolute, aliases)
    runtime = root / "opt/superplane/node-runtime"
    if runtime.exists():
        raise image.ImageRefused("target already contains a native runtime")
    install_tree(stage / "python", root, "/opt/superplane/node-runtime")
    install_tree(stage / "binaries", root, "/usr/bin")
    install_tree(stage / "cni", root, "/opt/cni/bin")
    for name in image.SOURCE_FILES:
        destination = (
            root
            / (
                "opt/superplane/bin"
                if "node-command/" in name
                else "opt/superplane/node-runtime"
            )
            / Path(name).name
        )
        ensure_directory(root, "/" + str(destination.parent.relative_to(root)))
        if destination.is_symlink():
            raise image.ImageRefused("maintained wrapper target is an alias")
        shutil.copyfile(image.APP_ROOT / name, destination)
        destination.chmod(0o755 if "node-command/" in name else 0o644)
    ensure_directory(root, "/var/lib/superplane")
    mask_target(root)
    measured = closure(root, plan["extra_runtime_files"], aliases)
    manifest = {"version": 1, "runtime": plan["runtime"], **measured}
    raw = image.runner.canonical(manifest).encode()
    if len(raw) > 65536:
        raise image.ImageRefused("generated manifest exceeds runtime bound")
    build.write(runtime / "manifest.json", manifest)
    descriptor = {
        "version": 1,
        "runtime_manifest": {**plan["runtime"], "artifact_sha256": image.sha(raw)},
        "bootstrap_wrapper_sha256": image.file_sha(
            runtime / "node_bootstrap_runner.py"
        ),
        "probe_wrapper_sha256": image.file_sha(runtime / "node_probe_runner.py"),
    }
    image.runner.validate_runtime_manifest(descriptor["runtime_manifest"])
    if len(image.runner.canonical(descriptor)) > 2000:
        raise image.ImageRefused("generated descriptor exceeds operation bound")
    # No target init, version probe, IMDS or bootstrap command. The only target
    # execution is its verified private Python importing/reading its own closure.
    for purpose, wrapper in (
        ("node-bootstrap", "node_bootstrap_runner.py"),
        ("node-api-dns-tls", "node_probe_runner.py"),
    ):
        contract = {
            "purpose": purpose,
            "runtime_manifest": descriptor["runtime_manifest"],
            "wrapper_sha256": image.file_sha(runtime / wrapper),
        }
        command(
            [
                "chroot",
                str(root),
                "/opt/superplane/node-runtime/bin/python3",
                "-I",
                "-B",
                "-S",
                "-c",
                "import sys,json;sys.path.insert(0,'/opt/superplane/node-runtime');import node_runner,node_bootstrap_runner,node_probe_runner;node_runner.verify_installation(json.loads(sys.argv[1]),sys.argv[2])",
                image.runner.canonical(contract),
                "/opt/superplane/node-runtime/" + wrapper,
            ],
            env=image.runner.clean_environment(),
        )
    refuse_enrollment(root)
    evidence.update(
        manifest=manifest,
        descriptor=descriptor,
        aliases=aliases,
        live_gpu_acceptance="pending",
    )
    return evidence


def run(plan, stage, build_id):
    if os.geteuid() != 0 or MOUNT.exists() or MOUNT.is_symlink():
        raise image.ImageRefused("isolated root helper and unused mountpoint required")
    if aws(plan, "sts", "get-caller-identity")["Account"] != plan["account_id"]:
        raise image.ImageRefused("helper account differs")
    instance = helper_identity()
    volumes = aws(
        plan,
        "ec2",
        "describe-volumes",
        "--filters",
        json.dumps(
            [
                {"Name": "attachment.instance-id", "Values": [instance]},
                {"Name": "attachment.device", "Values": [DEVICE]},
            ]
        ),
    )["Volumes"]
    volume = bound_volume(plan, build_id, instance, volumes)
    blockdevices = json.loads(
        command(
            [
                "lsblk",
                "--json",
                "--paths",
                "--output",
                "NAME,SERIAL,TYPE,FSTYPE,MOUNTPOINTS",
            ]
        )
    )["blockdevices"]
    candidates = device_for(volume["VolumeId"], blockdevices)
    MOUNT.mkdir(mode=0o700)
    found = []
    for candidate in candidates:
        options = (
            "ro,norecovery,nouuid" if candidate["fstype"] == "xfs" else "ro,noload"
        )
        command(
            [
                "mount",
                "-t",
                candidate["fstype"],
                "-o",
                options,
                candidate["name"],
                str(MOUNT),
            ]
        )
        try:
            if (
                target_path(MOUNT, "/etc/os-release").is_file()
                and target_path(MOUNT, "/usr").is_dir()
            ):
                refuse_enrollment(MOUNT)
                found.append(candidate)
        finally:
            command(["umount", str(MOUNT)])
    if len(found) != 1:
        raise image.ImageRefused(
            "target disk does not contain one supported root filesystem"
        )
    selected = found[0]
    bound_volume(
        plan,
        build_id,
        instance,
        aws(plan, "ec2", "describe-volumes", "--volume-ids", volume["VolumeId"])[
            "Volumes"
        ],
    )
    options = "rw,nouuid" if selected["fstype"] == "xfs" else "rw"
    command(
        ["mount", "-t", selected["fstype"], "-o", options, selected["name"], str(MOUNT)]
    )
    try:
        evidence = prepare_target(
            plan,
            stage,
            MOUNT,
            {
                "helper_instance_id": instance,
                "target_volume": volume,
                "root_partition": selected,
            },
        )
        bound_volume(
            plan,
            build_id,
            instance,
            aws(plan, "ec2", "describe-volumes", "--volume-ids", volume["VolumeId"])[
                "Volumes"
            ],
        )
        destination = stage / "evidence.json"
        build.write(destination, evidence)
        destination.chmod(0o644)
    finally:
        command(["sync", "-f", str(MOUNT)])
        command(["umount", str(MOUNT)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--build-id", required=True)
    args = parser.parse_args()
    from producer import read_plan

    run(read_plan(args.plan), Path(args.stage), args.build_id)


if __name__ == "__main__":
    main()
