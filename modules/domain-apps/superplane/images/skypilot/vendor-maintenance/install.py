"""Install only the locked Debian and Python maintenance packages without network access."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path("/maintenance")
lock = json.loads((root / "artifact-lock.json").read_text())
if "VERSION_CODENAME=trixie" not in Path("/etc/os-release").read_text():
    raise SystemExit("This package set requires Debian 13 (trixie)")
if (
    subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    != "amd64"
):
    raise SystemExit("This package set requires amd64")
for name, version in lock["python_versions"].items():
    if importlib.metadata.version(name) != version:
        raise SystemExit(f"Unexpected {name} version")


def inventory():
    output = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${db:Status-Status}\n"],
        text=True,
    )
    return {line.split("\t")[0]: line.split("\t")[1:] for line in output.splitlines()}


before = inventory()
python_before = {
    d.metadata["Name"].lower(): d.version for d in importlib.metadata.distributions()
}
selected = []
for item in lock["packages"]:
    if before.get(item["package"]) != [item["installed_version"], "installed"]:
        raise SystemExit(f"Unexpected installed package: {item['package']}")
    artifact = root / "artifacts" / item["file"]
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit(f"Checksum mismatch: {artifact.name}")
    actual = subprocess.check_output(
        ["dpkg-deb", "-f", str(artifact), "Package", "Version", "Architecture"],
        text=True,
    )
    expected = (
        f"Package: {item['package']}\nVersion: {item['version']}\n"
        f"Architecture: {item['architecture']}\n"
    )
    if actual != expected:
        raise SystemExit(f"Metadata mismatch: {artifact.name}")
    selected.append(str(artifact))
with tempfile.TemporaryDirectory(prefix="adp-offline-apt-") as temporary:
    temp = Path(temporary)
    (temp / "lists" / "partial").mkdir(parents=True)
    (temp / "sources.list").touch()
    (temp / "sources.list.d").mkdir()
    subprocess.run(
        [
            "apt-get",
            "-y",
            "--no-install-recommends",
            "-o",
            f"Dir::Etc::sourcelist={temp / 'sources.list'}",
            "-o",
            f"Dir::Etc::sourceparts={temp / 'sources.list.d'}",
            "-o",
            f"Dir::State::lists={temp / 'lists'}",
            "install",
            *selected,
        ],
        check=True,
        env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
    )
after = inventory()
expected_after = dict(before)
for item in lock["packages"]:
    expected_after[item["package"]] = [item["version"], "installed"]
if after != expected_after:
    raise SystemExit("Unexpected package set, versions or package state")
for item in lock["wheels"]:
    if importlib.metadata.version(item["package"]) != item["installed_version"]:
        raise SystemExit(f"Unexpected installed Python package: {item['package']}")
    artifact = root / "artifacts" / item["file"]
    if hashlib.sha256(artifact.read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit(f"Checksum mismatch: {artifact.name}")
subprocess.run(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        "--no-cache-dir",
        "--disable-pip-version-check",
        *[str(root / "artifacts" / item["file"]) for item in lock["wheels"]],
    ],
    check=True,
)
python_expected = dict(python_before)
for item in lock["wheels"]:
    python_expected[item["package"].lower()] = item["version"]
python_after = {
    d.metadata["Name"].lower(): d.version for d in importlib.metadata.distributions()
}
if python_after != python_expected:
    raise SystemExit("Unexpected Python distribution inventory change")
if subprocess.check_output(["dpkg", "--audit"], text=True).strip():
    raise SystemExit("dpkg audit failed")
subprocess.run(
    [sys.executable, "-m", "pip", "check"],
    check=True,
    env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
)
print(
    json.dumps(
        {
            "updated_packages": [x["package"] for x in lock["packages"]],
            "updated_wheels": [x["package"] for x in lock["wheels"]],
            "no_other_distribution_changes": True,
        }
    )
)
