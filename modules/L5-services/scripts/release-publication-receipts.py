"""Retain Transform publication receipts using the existing protected release authority."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from release_context import authorize_engine, release_identity, verify_principal


def aws(*arguments):
    return json.loads(subprocess.check_output(
        ["aws", *arguments, "--output", "json"], stderr=subprocess.PIPE, timeout=60,
    ))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def retain(directory, outcome, environment, *, read=aws, authorize=authorize_engine):
    identity = release_identity(environment)
    require(identity["service"] == "transform" and outcome in {"success", "failure", "skipped", "cancelled"},
            "Select this Transform publication outcome")
    directory = Path(directory)
    require(not directory.is_symlink() and directory.is_dir(), "Publication directory is unavailable")
    files, documents = {}, {}
    for name in ("publication-context.json", "release-artifacts.json", "validation-images.json"):
        path = directory / name
        if name == "validation-images.json" and not path.exists() and outcome != "success":
            continue
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 1048576,
                "Publication receipt is missing, linked or exceeds its bound")
        raw = path.read_bytes()
        require(len(raw) <= 1048576, "Publication receipt grew beyond its bound")
        documents[name] = json.loads(raw)
        files[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "content": raw.decode("utf-8")}
    require(documents["publication-context.json"] == identity, "Publication context belongs to another attempt")
    artifacts = documents["release-artifacts.json"]
    require(all(artifacts.get(key) == value for key, value in identity.items()),
            "Published artifacts belong to another attempt")
    bucket = environment.get("RELEASE_BUCKET", "")
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) and artifacts.get("bucket") == bucket,
            "Publication receipts require the selected private release bucket")
    hashes = {name: artifacts.get("artifacts", {}).get(name, {}).get("sha256", "") for name in ("wheel", "archive")}
    require(all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes.values()),
            "Publication artifact hashes are invalid")
    prefix = f"l5/security/{identity['source_revision']}/{hashes['wheel']}"
    selected = [(artifacts.get("manifest", {}), "manifest.json")]
    selected.extend((artifacts["artifacts"][name], filename) for name, filename in
                    (("wheel", "wheel.whl"), ("archive", "workers.zip")))
    require(all(record.get("key") == prefix + "/" + filename
                and isinstance(record.get("version_id"), str) and record["version_id"] not in {"", "None", "null"}
                for record, filename in selected), "Artifact coordinates differ from the selected immutable release")
    images = documents.get("validation-images.json")
    if images is not None:
        require(all(images.get(key) == value for key, value in identity.items())
                and images.get("wheel_sha256") == hashes["wheel"]
                and images.get("archive_sha256") == hashes["archive"]
                and set(images.get("images", {})) == {"provider", "checks"}
                and all(isinstance(value, str) and re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", value)
                        for value in images["images"].values()), "Image receipts differ from this publication")
    # Keep normal CI evidence on its IRSA identity. This narrowly scoped release
    # write uses the same selected role and l5/security prefix as publish-workers.
    authorize(environment, identity)
    verify_principal(environment, read("sts", "get-caller-identity"))
    key = (f"{prefix}/publication/{identity['repository_id']}/{identity['run_id']}/"
           f"{identity['run_attempt']}/transform-receipts.json")
    payload = {**identity, "publication_outcome": outcome, "images_published": images is not None,
               "qualification": False, "files": files}
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    with tempfile.TemporaryDirectory(prefix="l5-publication-receipts-") as temporary:
        source, observed = Path(temporary) / "receipts.json", Path(temporary) / "readback.json"
        source.write_bytes(raw)
        source.chmod(0o600)
        try:
            response = read("s3api", "put-object", "--bucket", bucket, "--key", key,
                            "--body", str(source), "--if-none-match", "*", "--server-side-encryption", "AES256")
        except subprocess.CalledProcessError as error:
            # An immutable replay is acceptable only after exact versioned
            # readback. Permission/network failures do not authorize a retry.
            message = error.stderr or b""
            if isinstance(message, bytes):
                message = message.decode(errors="replace")
            if not any(marker in message for marker in ("PreconditionFailed", "ConditionalRequestConflict")):
                raise
            response = read("s3api", "head-object", "--bucket", bucket, "--key", key)
        version = response.get("VersionId")
        require(isinstance(version, str) and version not in {"", "None", "null"},
                "Publication receipts require immutable storage versions")
        readback = read("s3api", "get-object", "--bucket", bucket, "--key", key,
                        "--version-id", version, str(observed))
        require(readback.get("VersionId") == version and observed.read_bytes() == raw,
                "Publication receipt readback differs from the original bytes or version")
    return {"bucket": bucket, "key": key, "version_id": version,
            "sha256": hashlib.sha256(raw).hexdigest(), "publication_outcome": outcome}


if __name__ == "__main__":
    try:
        directory, outcome = sys.argv[1:]
        receipt = retain(directory, outcome, os.environ)
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                for name in ("key", "version_id", "sha256"):
                    output.write(f"publication_receipt_{name}={receipt[name]}\n")
        print(json.dumps(receipt, sort_keys=True))
    except ValueError as error:
        raise SystemExit(str(error) + "; inspect the original publication attempt before retrying receipt retention. No qualification was recorded.") from None
    except subprocess.CalledProcessError:
        raise SystemExit("Transform receipt storage failed. Check the selected release role's write/version-read access to the existing release bucket and preserve this attempt's files; image publication must not be repeated merely to recover receipts. No qualification was recorded.") from None
