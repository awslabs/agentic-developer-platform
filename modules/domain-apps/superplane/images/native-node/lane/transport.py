"""Versioned native-build input transport; existing CodeBuild action owns execution."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build
import native_image as image
import producer
import source_provenance

MAX_ARCHIVE_BYTES = 4 * 1024**3
MAX_ARCHIVE_FILES = 200000


def aws(region, *args):
    return json.loads(
        subprocess.check_output(["aws", "--region", region, *args, "--output", "json"])
    )


def upload(region, bucket, key, path, expected_digest=None):
    digest = expected_digest or image.file_sha(path)
    image.digest(digest)
    if image.file_sha(path) != digest:
        raise image.ImageRefused("upload input differs from approved digest")
    checksum = base64.b64encode(bytes.fromhex(digest)).decode()
    result = aws(
        region,
        "s3api",
        "put-object",
        "--bucket",
        bucket,
        "--key",
        key,
        "--body",
        str(path),
        "--checksum-sha256",
        checksum,
    )
    if result.get("ChecksumSHA256") != checksum or image.file_sha(path) != digest:
        raise image.ImageRefused("uploaded bytes differ from approved digest")
    version = result.get("VersionId")
    if not version or version == "null":
        raise image.ImageRefused("versioned immutable input required")
    return {
        "bucket": bucket,
        "key": key,
        "version": version,
        "sha256": digest,
    }


def download(region, pointer, target, bucket):
    image.exact(pointer, {"bucket", "key", "version", "sha256"})
    image.digest(pointer["sha256"])
    if (
        pointer["bucket"] != bucket
        or not pointer["key"].startswith("native-input/")
        or pointer["version"] in {"", "null"}
    ):
        raise image.ImageRefused("input object scope/version differs")
    target.parent.mkdir(parents=True, exist_ok=True)
    aws(
        region,
        "s3api",
        "get-object",
        "--bucket",
        bucket,
        "--key",
        pointer["key"],
        "--version-id",
        pointer["version"],
        str(target),
    )
    if image.file_sha(target) != pointer["sha256"]:
        raise image.ImageRefused("input object digest differs")


def extract(archive, destination):
    """Validate every ZIP entry before writing; create symlinks last, never follow them."""
    with zipfile.ZipFile(archive) as source:
        entries, symlinks, total = {}, {}, 0
        for entry in source.infolist():
            name = entry.filename.rstrip("/")
            path = PurePosixPath(name)
            if (
                not name
                or str(path) != name
                or path.is_absolute()
                or any(p in {".", "..", ".git"} for p in path.parts)
                or name in entries
            ):
                raise image.ImageRefused("unsafe/duplicate source archive path")
            mode = entry.external_attr >> 16
            kind = stat.S_IFMT(mode)
            if kind not in {0, stat.S_IFREG, stat.S_IFDIR, stat.S_IFLNK}:
                raise image.ImageRefused("special source archive member")
            total += entry.file_size
            if total > MAX_ARCHIVE_BYTES or len(entries) >= MAX_ARCHIVE_FILES:
                raise image.ImageRefused("source archive exceeds bound")
            entries[name] = entry
            if kind == stat.S_IFLNK:
                if entry.file_size > 4096:
                    raise image.ImageRefused("source symlink exceeds bound")
                symlinks[name] = source.read(entry)
        for name in entries:
            if any(str(parent) in symlinks for parent in PurePosixPath(name).parents):
                raise image.ImageRefused("source archive has symlink parent")
        destination.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, entry in entries.items():
            target = destination / name
            if name in symlinks:
                continue
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as incoming, target.open("xb") as output:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
            target.chmod(0o755 if (entry.external_attr >> 16) & 0o111 else 0o644)
        for name, payload in symlinks.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(os.fsdecode(payload), target)


def approved_plan(path, digest):
    image.digest(digest)
    with Path(path).open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != digest:
        raise image.ImageRefused("approved plan digest differs")
    value = image.runner.decode_json(raw)
    if raw.decode() != image.runner.canonical(value):
        raise image.ImageRefused("canonical producer plan required")
    return producer.validate_plan(value), raw


def freeze(source, destination, digest):
    image.digest(digest)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    actual = hashlib.sha256()
    with Path(source).open("rb") as incoming, destination.open("xb") as output:
        while chunk := incoming.read(1024 * 1024):
            actual.update(chunk)
            output.write(chunk)
        output.flush()
        os.fsync(output.fileno())
    if actual.hexdigest() != digest:
        raise image.ImageRefused("staged input differs from approved digest")
    destination.chmod(0o400)
    return destination


def prepare(args):
    checkout = Path(args.checkout).resolve()
    plan, plan_raw = approved_plan(args.plan, args.plan_sha256)
    if (plan["account_id"], plan["region"]) != (args.account_id, args.region):
        raise image.ImageRefused("approved dispatch account/region differs")
    if (
        aws(plan["region"], "sts", "get-caller-identity")["Account"]
        != plan["account_id"]
    ):
        raise image.ImageRefused("dispatcher account differs")
    output = Path(args.output).resolve()
    if output.is_relative_to(checkout):
        raise image.ImageRefused("dispatch output must be outside checkout")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    staged_plan = output / "plan.json"
    staged_plan.write_bytes(plan_raw)
    staged_plan.chmod(0o400)
    attestation = output / "source-attestation.json"
    build.write(
        attestation, source_provenance.create(checkout, plan["source_revision"])
    )
    if image.file_sha(attestation) != plan["source_attestation_sha256"]:
        raise image.ImageRefused("approved source attestation differs")
    archive = output / "source.zip"
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "archive",
            "--format=zip",
            "--output",
            str(archive),
            plan["source_revision"],
        ],
        check=True,
    )
    extracted = output / "source-check"
    extract(archive, extracted)
    source_provenance.verify(
        extracted,
        attestation,
        plan["source_attestation_sha256"],
        plan["source_revision"],
    )
    image.pattern(args.dispatch_id, r"[A-Za-z0-9_-]{1,150}")
    prefix = "native-input/" + args.dispatch_id + "/"
    files = {
        "source.zip": (archive, image.file_sha(archive)),
        "source-attestation.json": (attestation, plan["source_attestation_sha256"]),
        "plan.json": (staged_plan, args.plan_sha256),
    }
    for name, value in plan["upstream"].items():
        if name in {"python", "nodeadm", "crictl"}:
            path = Path(args.inputs) / value["file"]
            if image.file_sha(path) != value["sha256"]:
                raise image.ImageRefused("upstream archive digest differs")
            files["inputs/" + value["file"]] = (
                freeze(path, output / "frozen-inputs" / value["file"], value["sha256"]),
                value["sha256"],
            )
    for name, path in (
        ("packer", Path(args.packer)),
        ("amazon_plugin", Path(args.amazon_plugin)),
    ):
        if image.file_sha(path) != plan["tools"][name]["sha256"]:
            raise image.ImageRefused("tool digest differs")
        digest = plan["tools"][name]["sha256"]
        files[name] = (freeze(path, output / "frozen-tools" / name, digest), digest)
    approved_plan(args.plan, args.plan_sha256)
    envelope = {
        "version": 1,
        "dispatch_id": args.dispatch_id,
        "account_id": plan["account_id"],
        "region": plan["region"],
        "source_revision": plan["source_revision"],
        "approved_plan_sha256": args.plan_sha256,
        "objects": {
            name: upload(
                plan["region"], args.bucket, prefix + name, path, expected_digest=digest
            )
            for name, (path, digest) in files.items()
        },
    }
    build.write(output / "envelope.json", envelope)
    pointer = upload(
        plan["region"], args.bucket, prefix + "envelope.json", output / "envelope.json"
    )
    build.write(
        output / "dispatch.json",
        {"envelope": pointer, "status": "START_PENDING", "build_id": None},
    )
    print(json.dumps(pointer, sort_keys=True))


def receive(args):
    root = Path(args.output)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    pointer = json.loads(args.envelope)
    download(args.region, pointer, root / "envelope.json", args.bucket)
    envelope = json.loads((root / "envelope.json").read_text())
    image.exact(
        envelope,
        {
            "version",
            "dispatch_id",
            "account_id",
            "region",
            "source_revision",
            "approved_plan_sha256",
            "objects",
        },
    )
    image.pattern(envelope["dispatch_id"], r"[A-Za-z0-9_-]{1,150}")
    if (
        envelope["version"] != 1
        or envelope["account_id"] != args.account_id
        or envelope["region"] != args.region
    ):
        raise image.ImageRefused("dispatch scope differs")
    for name, value in envelope["objects"].items():
        path = PurePosixPath(name)
        if (
            str(path) != name
            or path.is_absolute()
            or any(p in {".", ".."} for p in path.parts)
        ):
            raise image.ImageRefused("invalid staged input name")
        download(args.region, value, root / "download" / name, args.bucket)
    staged = root / "download"
    plan, _ = approved_plan(staged / "plan.json", envelope["approved_plan_sha256"])
    allowed = json.loads(args.constraints)
    expected = {
        "account_id": plan["account_id"],
        "region": plan["region"],
        "helper_ami_id": plan["helper"]["ami_id"],
        "subnet_id": plan["builder"]["subnet_id"],
        "security_group_id": plan["builder"]["security_group_id"],
        "instance_profile": plan["builder"]["instance_profile"],
        "instance_type": plan["builder"]["instance_type"],
        "kms_key_id": plan["builder"]["kms_key_id"],
    }
    if allowed != expected or plan["source_revision"] != envelope["source_revision"]:
        raise image.ImageRefused("plan differs from dedicated project constraints")
    extract(staged / "source.zip", root / "source")
    source_provenance.verify(
        root / "source",
        staged / "source-attestation.json",
        plan["source_attestation_sha256"],
        plan["source_revision"],
    )
    for name in ("packer", "amazon_plugin"):
        (staged / name).chmod(0o700)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    prep = subs.add_parser("prepare")
    for name in (
        "checkout",
        "account-id",
        "region",
        "plan",
        "plan-sha256",
        "inputs",
        "packer",
        "amazon-plugin",
        "output",
        "bucket",
        "dispatch-id",
    ):
        prep.add_argument("--" + name, required=True)
    recv = subs.add_parser("receive")
    for name in ("output", "envelope", "bucket", "region", "account-id", "constraints"):
        recv.add_argument("--" + name, required=True)
    args = parser.parse_args()
    (prepare if args.command == "prepare" else receive)(args)


if __name__ == "__main__":
    main()
