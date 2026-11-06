#!/usr/bin/env python3
"""Update changed Lambda archives, retaining revision and completion checks."""

import argparse
import base64
import hashlib
import json
import subprocess
from pathlib import Path


def aws(arguments):
    result = subprocess.run(["aws", "lambda", *arguments, "--output", "json"], capture_output=True, text=True, timeout=900)
    if result.returncode:
        if arguments[0] == "get-function-configuration" and "ResourceNotFoundException" in result.stderr:
            return None
        raise RuntimeError(f"Lambda {arguments[0]} failed")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def deploy(function, archive, call=aws):
    expected = base64.b64encode(hashlib.sha256(archive.read_bytes()).digest()).decode()

    def configuration():
        return call(["get-function-configuration", "--function-name", function])

    def wait():
        call(["wait", "function-updated-v2", "--function-name", function])

    current = configuration()
    if current is None:
        return "not provisioned"
    if current.get("LastUpdateStatus") == "InProgress":
        wait()
        current = configuration()
    if not current or not current.get("RevisionId"):
        raise RuntimeError("Lambda configuration disappeared or lacks revision")
    if current.get("CodeSha256") == expected and current.get("LastUpdateStatus") == "Successful" and current.get("State") == "Active":
        return "unchanged"
    call(
        [
            "update-function-code",
            "--function-name",
            function,
            "--zip-file",
            f"fileb://{archive.resolve()}",
            "--revision-id",
            current["RevisionId"],
        ]
    )
    wait()
    final = configuration()
    if not final or final.get("CodeSha256") != expected or final.get("LastUpdateStatus") != "Successful" or final.get("State") != "Active":
        raise RuntimeError("Lambda update did not converge to the requested archive")
    return "updated"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    print(f"Lambda archive: {deploy(args.function, args.archive)}")
