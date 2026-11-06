"""Package pinned Fleeting and bootstrap inputs from a reviewed runner release lock."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class BuildError(ValueError):
    pass


def release_inputs(lock, region, bucket):
    try:
        host = lock["host_ami"][region]
        plugin = lock["fleeting_plugin"]
        image_id, owner = host["image_id"], host["owner_id"]
        key = plugin["s3_key"]
        if not re.fullmatch(r"ami-[0-9a-f]{8,17}", image_id):
            raise BuildError("host AMI ID must be pinned")
        if not re.fullmatch(r"[0-9]{12}", owner):
            raise BuildError("host AMI owner must be pinned")
        if host["architecture"] not in ("x86_64", "arm64"):
            raise BuildError("unsupported host architecture")
        if not re.fullmatch(r"gitlab-runners/upstream/[A-Za-z0-9_.-]+", key) or "latest" in key.lower():
            raise BuildError("plugin must be in the dedicated upstream prefix")
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", bucket):
            raise BuildError("approved private source bucket is required")
        if not SHA256.fullmatch(plugin["sha256"]) or not SHA256.fullmatch(host["bootstrap_sha256"]):
            raise BuildError("artifact hashes must be full reviewed SHA-256 digests")
    except (KeyError, TypeError) as error:
        raise BuildError("missing pinned host AMI, Fleeting plugin or bootstrap release fields") from error
    return host, plugin


def bootstrap_base(lock):
    image = lock.get("bootstrap_python_image")
    if not isinstance(image, str) or not re.fullmatch(r"python:3\.12\.[0-9]+-slim@sha256:[0-9a-f]{64}", image):
        raise BuildError("bootstrap Python base must have a reviewed immutable digest")
    digest = lock.get("bootstrap_requirements_sha256")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise BuildError("bootstrap dependencies must have a reviewed lock hash")
    dependencies = ROOT / "bootstrap" / "requirements.lock"
    if not dependencies.is_file():
        raise BuildError("bootstrap dependency lock is missing")
    verify_digest(dependencies, digest)
    return image


def checked_aws(*args):
    result = subprocess.run(["aws", *args], capture_output=True, check=False)
    if result.returncode:
        raise BuildError("scoped AWS verification or artifact transfer failed")
    return result.stdout


def verify_ami(host, region):
    response = json.loads(checked_aws("ec2", "describe-images", "--region", region,
                                      "--owners", host["owner_id"], "--image-ids", host["image_id"],
                                      "--output", "json"))
    images = response.get("Images", [])
    if len(images) != 1 or any((images[0].get("ImageId") != host["image_id"],
                               images[0].get("OwnerId") != host["owner_id"],
                               images[0].get("Architecture") != host["architecture"],
                               images[0].get("State") != "available",
                               images[0].get("Public") is not False)):
        raise BuildError("host AMI owner, architecture or readiness does not match reviewed lock")


def verify_digest(path, expected):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise BuildError("release artifact checksum does not match reviewed lock")


def prepare_plugin(lock, region, bucket, output):
    """Stage the reviewed executable before image build; no produced AMI is needed."""
    try:
        plugin = lock["fleeting_plugin"]
        if (plugin["version"] != "v1.3.1"
                or plugin["source_commit"] != "bf5cbcc9183e164637cb4dae42e64e49365e8ade"
                or plugin["s3_key"] != "gitlab-runners/upstream/fleeting-plugin-aws-linux-amd64"
                or plugin["sha256"] != "2090afe6b2a888d1e58990dbf7b731124956d27358ff8b8532180235d9d49ebb"
                or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", bucket)
                or not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]+", region)):
            raise BuildError("reviewed amd64 connector pin or source scope is invalid")
    except (KeyError, TypeError) as error:
        raise BuildError("reviewed connector release fields are missing") from error
    raw = checked_aws("s3", "cp", f"s3://{bucket}/{plugin['s3_key']}", "-", "--region", region)
    stage_plugin(raw, plugin["sha256"], output)
    return plugin["sha256"]


def stage_plugin(raw, expected, output):
    if hashlib.sha256(raw).hexdigest() != expected:
        raise BuildError("connector checksum differs from reviewed release")
    if (len(raw) < 64 or raw[:7] != b"\x7fELF\x02\x01\x01"
            or int.from_bytes(raw[18:20], "little") != 62):
        raise BuildError("connector must be the reviewed Linux amd64 ELF executable")
    # Caller supplies a fresh isolated Docker context, never a source-tree path.
    if output.exists() or not output.parent.is_dir():
        raise BuildError("connector output must be absent in an existing build context")
    with output.open("xb") as stream:
        stream.write(raw)
    output.chmod(0o555)


def build(lock_path, region, bucket, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise BuildError("build requires the reviewed source commit")
    try:
        lock = json.loads(lock_path.read_text())
        host, plugin = release_inputs(lock, region, bucket)
        bootstrap_base(lock)
    except (OSError, json.JSONDecodeError) as error:
        raise BuildError("reviewed S2 release lock is missing or invalid") from error
    bootstrap = ROOT / "templates" / "job-user-data.sh.tftpl"
    if not bootstrap.is_file():
        raise BuildError("S2 host bootstrap artifact is missing")
    verify_digest(bootstrap, host["bootstrap_sha256"])
    verify_ami(host, region)
    with tempfile.TemporaryDirectory() as directory:
        plugin_file = Path(directory) / "fleeting-plugin"
        plugin_file.write_bytes(checked_aws("s3", "cp", f"s3://{bucket}/{plugin['s3_key']}", "-", "--region", region))
        verify_digest(plugin_file, plugin["sha256"])
        manifest = json.dumps({"source_sha": revision, "region": region, "host_ami": host["image_id"],
                               "bootstrap_sha256": host["bootstrap_sha256"], "plugin_sha256": plugin["sha256"]},
                              sort_keys=True).encode()
        archive = Path(directory) / "runner-host-inputs.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            bundle.add(bootstrap, arcname="job-user-data.sh.tftpl")
            bundle.add(plugin_file, arcname="fleeting-plugin")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(manifest)
            bundle.addfile(info, io.BytesIO(manifest))
        checked_aws("s3", "cp", str(archive),
                    f"s3://{bucket}/gitlab-runners/artifacts/{revision}/{region}/runner-host-inputs.tar.gz",
                    "--region", region, "--only-show-errors")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["base"]:
            print(bootstrap_base(json.loads((ROOT / "versions.lock.json").read_text())))
        elif sys.argv[1:] == ["build"]:
            build(ROOT / "versions.lock.json", os.environ["AWS_REGION"],
                  os.environ["RUNNER_SOURCE_BUCKET"], os.environ["ADP_SOURCE_SHA"])
        elif len(sys.argv) == 3 and sys.argv[1] == "prepare-plugin":
            print(prepare_plugin(json.loads((ROOT / "versions.lock.json").read_text()),
                                 os.environ["AWS_REGION"], os.environ["RUNNER_SOURCE_BUCKET"],
                                 Path(sys.argv[2])))
        else:
            raise BuildError("expected base, build or prepare-plugin command")
    except (BuildError, KeyError, OSError, json.JSONDecodeError) as error:
        sys.exit(f"Runner build refused: {error}")
