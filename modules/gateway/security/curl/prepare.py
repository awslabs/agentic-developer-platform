"""Fetch checksum-pinned, signed Debian source and apply the reviewed curl fix."""

import argparse
import hashlib
import http.client
import json
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

BUNDLE = Path(__file__).parent
LOCK = json.loads((BUNDLE / "source-lock.json").read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download(destination):
    destination.mkdir(parents=True, exist_ok=True)
    for name, source in LOCK["files"].items():
        target = destination / name
        url = urlsplit(source["url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise RuntimeError("Debian source URL must use HTTPS without credentials")
        connection = http.client.HTTPSConnection(url.hostname, url.port or 443, timeout=60)
        try:
            connection.request("GET", url.path + ("?" + url.query if url.query else ""))
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"Unexpected Debian source HTTP status: {response.status}")
            content = response.read(source["size"] + 1)
            if len(content) != source["size"] or hashlib.sha256(content).hexdigest() != source["sha256"]:
                raise RuntimeError(f"Unexpected Debian source bytes: {name}")
            target.write_bytes(content)
        finally:
            connection.close()
        if digest(target) != source["sha256"]:
            raise RuntimeError(f"Unexpected Debian source bytes: {name}")


def prepare(destination):
    dsc = destination / f"curl_{LOCK['source_version']}.dsc"
    for name, source in LOCK["files"].items():
        if digest(destination / name) != source["sha256"]:
            raise RuntimeError(f"Unexpected Debian source bytes: {name}")
    source_dir = destination / "curl-patched"
    subprocess.run(["dpkg-source", "-x", str(dsc), str(source_dir)], check=True)
    cookie = source_dir / "lib/cookie.c"
    if digest(cookie) != LOCK["cookie_source_sha256"]["before"]:
        raise RuntimeError("Unexpected baseline cookie source")
    patch = source_dir / "debian/patches/CVE-2026-8924.patch"
    shutil.copyfile(BUNDLE / patch.name, patch)
    with (source_dir / "debian/patches/series").open("a") as series:
        series.write("\nCVE-2026-8924.patch\n")
    subprocess.run(
        ["quilt", "push"],
        cwd=source_dir,
        env={**os.environ, "QUILT_PATCHES": "debian/patches", "QUILT_PATCH_OPTS": "--fuzz=0"},
        check=True,
    )
    if digest(cookie) != LOCK["cookie_source_sha256"]["after"]:
        raise RuntimeError("Unexpected patched cookie source")
    changelog = source_dir / "debian/changelog"
    changelog.write_text(
        f"curl ({LOCK['package_version']}) unstable; urgency=medium\n\n"
        "  * Apply upstream CVE-2026-8924 PSL trailing-dot fix and regression1629.\n"
        "    Preserve Debian configure options and runtime protocol/features.\n\n"
        " -- Security Maintenance <security-maintenance@users.noreply.github.com>  "
        "Sat, 26 Sep 2026 07:00:00 +0000\n\n" + changelog.read_text()
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["download", "prepare"])
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    globals()[args.action](args.destination.resolve())
