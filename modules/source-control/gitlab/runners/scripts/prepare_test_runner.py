#!/usr/bin/env python3
"""Install the reviewed Runner binary for daemon-free, offline registration tests."""
import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEST_BINARY_SHA256 = "1c1752101f5b0f212615ad2c674d8f3499de96d9377f31aed1e3eecdbd068de1"
MANAGER_IMAGE = "gitlab/gitlab-runner:alpine-v19.5.2@sha256:800157105b792ed3f0c9bc0aa6bc6427d66889cafd2c8aae4c4c177194699144"
MAX_BINARY_BYTES = 160 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        raise ValueError("runner_test_redirect_refused")


def prepare(lock, output):
    version = lock["chart"]["runner_version"]
    image = lock["manager_image"]
    binary_lock = lock["test_runner_binary"]
    source = f"https://gitlab-runner-downloads.s3.amazonaws.com/v{version}/binaries/gitlab-runner-linux-amd64"
    if (version != "19.5.2" or image != MANAGER_IMAGE
            or binary_lock != {"source": source, "sha256": TEST_BINARY_SHA256}):
        raise ValueError("runner_test_binary_not_pinned")
    if output.exists() or output.is_symlink():
        raise ValueError("runner_test_output_exists")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    opener = urllib.request.build_opener(NoRedirect())
    with tempfile.TemporaryDirectory(prefix="runner-download-", dir=output.parent) as directory:
        binary = Path(directory) / "gitlab-runner"
        checksum = hashlib.sha256()
        size = 0
        with opener.open(source, timeout=30) as response, binary.open("xb") as destination:
            if response.status != 200:
                raise ValueError("runner_test_download_failed")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_BINARY_BYTES:
                    raise ValueError("runner_test_binary_too_large")
                checksum.update(chunk)
                destination.write(chunk)
        if not size or checksum.hexdigest() != TEST_BINARY_SHA256:
            raise ValueError("runner_test_checksum_wrong")
        binary.chmod(0o700)
        observed = subprocess.run([str(binary), "--version"], check=True, capture_output=True,
                                  timeout=15).stdout.decode()
        if (not re.search(r"^Version:\s+" + re.escape(version) + r"$", observed, re.MULTILINE)
                or not re.search(r"^OS/Arch:\s+linux/amd64$", observed, re.MULTILINE)):
            raise ValueError("runner_test_version_wrong")
        os.link(binary, output)
    return {"manager_image": image, "version": version, "binary_sha256": checksum.hexdigest(),
            "path": str(output.resolve()), "scope": "reviewed binary for offline tests"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(json.loads((ROOT / "versions.lock.json").read_text()), args.output)
    except Exception:
        print(json.dumps({"result": "failed", "reason": "pinned_runner_extraction_failed"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
