#!/usr/bin/env python3
"""Reject incomplete reviewed runner release inputs before touching Terraform state."""

import argparse
import json
from pathlib import Path
import sys


RUNNERS = Path(__file__).resolve().parents[1] / "runners"
sys.path.insert(0, str(RUNNERS / "scripts"))
from build_artifacts import BuildError, bootstrap_base, release_inputs, verify_digest
from release_lock import load_lock


def check(root, region, bucket, lock_path=None, lock_sha256=None):
    try:
        if lock_path:
            lock = load_lock(lock_path, lock_sha256)
        else:
            if lock_sha256:
                raise BuildError("S2 runner release lock digest requires a private file")
            if not root.joinpath("versions.lock.json").is_file():
                raise BuildError("S2 runner release lock is missing")
            lock = json.loads(root.joinpath("versions.lock.json").read_text())
    except BuildError:
        raise
    except (OSError, ValueError) as error:
        raise BuildError("S2 runner release lock is invalid") from error
    host, _ = release_inputs(lock, region, bucket)
    bootstrap_base(lock)
    template = root / "templates" / "job-user-data.sh.tftpl"
    if not template.is_file():
        raise BuildError("S2 host bootstrap is missing")
    verify_digest(template, host["bootstrap_sha256"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--release-lock")
    parser.add_argument("--release-lock-sha256")
    args = parser.parse_args()
    try:
        check(RUNNERS, args.region, args.bucket, args.release_lock, args.release_lock_sha256)
    except BuildError as error:
        parser.exit(1, f"Runner preflight refused: {error}\n")


if __name__ == "__main__":
    main()
