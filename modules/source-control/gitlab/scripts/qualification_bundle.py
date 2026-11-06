#!/usr/bin/env python3
"""Open an approved private qualification bundle and run the existing live verifier."""

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {"fixtures.json", "evidence.json", "settings.json", "ca.pem", "kubeconfig", "release-lock.json"}


def require(condition):
    if not condition:
        raise ValueError("private qualification binding or receipt invalid")


def private(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o600)
    return path.read_bytes()


def prepare(archive, directory, args):
    require(re.fullmatch(r"[0-9a-f]{64}", args.bundle_sha256))
    require(archive.stat().st_size <= 32 * 1024 * 1024)
    require(hashlib.sha256(private(archive)).hexdigest() == args.bundle_sha256)
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700 and not list(directory.iterdir()))
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = [item.name for item in members]
        require(len(names) <= 128 and sum(item.size for item in members) <= 64 * 1024 * 1024
                and len(names) == len(set(names)) and FILES <= set(names)
                and all(item.isfile() and 0 < item.size <= 8 * 1024 * 1024
                        and (item.name in FILES or re.fullmatch(r"host-history/[a-z0-9_-]+\.json", item.name))
                        for item in members)
                and any(name.startswith("host-history/") for name in names))
        for member in members:
            target = directory / member.name
            target.parent.mkdir(mode=0o700, exist_ok=True)
            with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "wb") as output:
                output.write(bundle.extractfile(member).read())
    fixtures = json.loads(private(directory / "fixtures.json"))
    settings = json.loads(private(directory / "settings.json"))
    require(fixtures.get("run_id") == args.run_id and fixtures.get("source_revision") == args.source_revision
            and fixtures.get("account_id") == args.account_id and settings.get("region") == args.region
            and settings.get("expected_role_arn") == args.expected_role
            and set(settings) == {"region", "expected_role_arn", "gitlab_url", "admin_secret_arn", "cluster", "manager_deployment"}
            and all(isinstance(value, str) and value for value in settings.values())
            and hashlib.sha256(private(directory / "release-lock.json")).hexdigest() == args.release_lock_sha256)


def verify(directory, args, *, env=None, s3=None):
    fixtures = json.loads(private(directory / "fixtures.json"))
    settings = json.loads(private(directory / "settings.json"))
    require(hashlib.sha256(private(directory.parent / "bundle.tar.gz")).hexdigest() == args.bundle_sha256
            and hashlib.sha256(private(directory / "release-lock.json")).hexdigest() == args.release_lock_sha256)
    require(fixtures.get("run_id") == args.run_id and fixtures.get("source_revision") == args.source_revision
            and fixtures.get("account_id") == args.account_id and settings.get("region") == args.region
            and settings.get("expected_role_arn") == args.expected_role)
    histories = sorted((directory / "host-history").glob("*.json"))
    require(histories and all(private(path) for path in histories))
    command = ["python3", str(ROOT / "tests/acceptance/verify.py"), "--fixtures", str(directory / "fixtures.json"),
               "--evidence", str(directory / "evidence.json"), "--region", args.region,
               "--expected-role", args.expected_role, "--gitlab-url", settings["gitlab_url"],
               "--admin-secret-arn", settings["admin_secret_arn"], "--ca-file", str(directory / "ca.pem"),
               "--cluster", settings["cluster"], "--kubeconfig", str(directory / "kubeconfig"),
               "--manager-deployment", settings["manager_deployment"], "--release-lock-file",
               str(directory / "release-lock.json"), "--release-lock-sha256", args.release_lock_sha256,
               "--stage", "final"]
    for history in histories:
        command.extend(("--host-history", str(history)))
    completed = subprocess.run(command, capture_output=True, timeout=900, check=False)
    require(completed.returncode == 0 and len(completed.stdout) < 4096)
    result = json.loads(completed.stdout)
    require(result.get("verdict") == "pass" and result.get("fresh_independent_api_readbacks") == "passed")
    from qualification_delivery import publish
    import boto3
    result = publish(result, args, os.environ if env is None else env,
                     boto3.client("s3", region_name=args.region) if s3 is None else s3)
    with os.fdopen(os.open(directory / "final-verdict.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), "w") as output:
        json.dump(result, output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--archive", type=Path)
    for name in ("bundle-sha256", "release-lock-sha256", "run-id", "source-revision",
                 "account-id", "region", "expected-role"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--workflow-run-id", type=int)
    args = parser.parse_args()
    try:
        if args.archive:
            prepare(args.archive, args.directory, args)
        else:
            require(args.workflow_run_id is not None and args.workflow_run_id > 0)
            verify(args.directory, args)
    except Exception:
        print("Private qualification incomplete")
        return 1
    print("Private qualification verified" if not args.archive else "Private qualification bundle bound")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
