#!/usr/bin/env python3
"""Extract only committed regular files into a fresh private build directory."""

import argparse
import io
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile


def prepare(repository, sha, parent):
    if not (re.fullmatch(r"[a-f0-9]{40}", sha)):
        raise AssertionError()
    if not (
        subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True
        ).strip()
        == sha
    ):
        raise AssertionError()
    body = subprocess.check_output(["git", "archive", sha], cwd=repository)
    with tarfile.open(fileobj=io.BytesIO(body)) as source:
        if not (
            all(
                not m.issym() and not m.islnk() and (m.isfile() or m.isdir())
                for m in source
            )
        ):
            raise AssertionError("Non-regular committed input refused")
        root = Path(tempfile.mkdtemp(prefix="webhook-source.", dir=parent))
        source.extractall(root, filter="data")
    return root


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=".")
    args = parser.parse_args()
    root = prepare(args.repository, os.environ["GITHUB_SHA"], os.environ["RUNNER_TEMP"])
    with open(os.environ["GITHUB_ENV"], "a") as output:
        output.write(f"CLEAN_SOURCE={root}\n")
        output.write(
            f"RECEIPT={os.environ['RUNNER_TEMP']}/webhook-{os.environ['GITHUB_RUN_ID']}/receipt.json\n"
        )


if __name__ == "__main__":
    main()
