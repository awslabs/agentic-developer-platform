"""Read back host readiness without accepting an unscoped service status."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path


def _trusted_json(path: Path, uid: int) -> dict:
    parent = path.parent.lstat()
    entry = path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid or parent.st_mode & 0o022
            or not stat.S_ISREG(entry.st_mode) or entry.st_uid != uid or entry.st_mode & 0o022):
        raise ValueError("untrusted Crew readiness receipt")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode) or current.st_uid != uid or current.st_mode & 0o022:
            raise ValueError("untrusted Crew readiness receipt")
        with os.fdopen(descriptor, "r", closefd=False) as source:
            value = json.load(source)
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise ValueError("invalid Crew readiness receipt")
    return value


def verify(scope: dict, *, root: Path = Path("/run/adp"),
           profile: Path = Path("/opt/adp/crew/current/profile.json"), uid: int = 0,
           run=subprocess.run) -> bool:
    fields = ("run_id", "tenant_id", "user_id", "box_id", "instance_ref", "private_ip",
              "profile_revision", "lease_generation", "restore_manifest_version")
    if set(scope) != ((set(fields) - {"run_id"}) | {"run_id_sha256", "checkpoint_id", "manifest_sha256"}):
        return False
    # A first host is verified with a fresh scope (#8171): no checkpoint pins and manifest
    # version 0. Any other mix of pins and version is neither fresh nor a restore.
    fresh = scope["checkpoint_id"] is None
    if (type(scope["lease_generation"]) is not int or scope["lease_generation"] < 1
            or type(scope["restore_manifest_version"]) is not int
            or any(not isinstance(scope[field], str) or not scope[field] for field in
                   ("tenant_id", "user_id", "box_id", "instance_ref", "private_ip", "profile_revision"))
            or not isinstance(scope["run_id_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", scope["run_id_sha256"])):
        return False
    if fresh:
        if scope["manifest_sha256"] is not None or scope["restore_manifest_version"] != 0:
            return False
    elif (scope["restore_manifest_version"] < 1
            or not isinstance(scope["checkpoint_id"], str) or not scope["checkpoint_id"]
            or not isinstance(scope["manifest_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", scope["manifest_sha256"])):
        return False
    try:
        authority = _trusted_json(root / "crew-host.json", uid)
        ready = _trusted_json(root / "crew-ready", uid)
        restored = _trusted_json(root / "crew-restore.json", uid)
        installed = _trusted_json(profile.resolve(strict=True), uid)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return False
    if (any(authority.get(field) != scope[field] or ready.get(field) != scope[field] for field in fields if field != "run_id")
            or not isinstance(authority.get("run_id"), str)
            or hashlib.sha256(authority["run_id"].encode()).hexdigest() != scope["run_id_sha256"]
            or ready.get("run_id") != authority["run_id"]
            or set(restored) != {"checkpoint_id", "manifest_sha256"}
            or any(restored[field] != scope[field] for field in ("checkpoint_id", "manifest_sha256"))
            or installed.get("revision") != scope["profile_revision"]
            or run(["systemctl", "is-active", "--quiet", "adp-crew.service"], check=False).returncode
            or run(["systemctl", "is-active", "--quiet", "adp-crew-host.service"], check=False).returncode):
        return False
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", required=True)
    arguments = parser.parse_args()
    if os.geteuid() != 0 or not verify(json.loads(arguments.scope)):
        raise SystemExit(1)
    print("crew-ready-ok")


if __name__ == "__main__":
    main()
