"""Fetch hash-locked vendor source/binaries and isolated Maven build dependencies."""

import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parent


def fetch(url, destination, expected):
    if (
        destination.is_file()
        and hashlib.sha256(destination.read_bytes()).hexdigest() == expected
    ):
        return
    with urllib.request.urlopen(url, timeout=60) as response:
        raw = response.read()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("download digest differs from reviewed artifact lock")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(raw)


def main():
    lock = json.loads((ROOT / "artifact-lock.json").read_text())
    for item in lock["artifacts"]:
        fetch(item["url"], ROOT / "artifacts" / item["filename"], item["sha256"])
    tools = json.loads((ROOT / "tool-lock.json").read_text())
    for name, digest in tools["files"].items():
        fetch(tools["repository"] + name, ROOT / ".m2" / name, digest)
    print("Verified vendor artifacts and offline build dependency cache")


if __name__ == "__main__":
    main()
