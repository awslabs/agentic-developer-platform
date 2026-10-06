"""Install only the locked Debian maintenance packages without network access."""

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


before_commands = {
    str(path)
    for directory in ("/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin")
    for path in Path(directory).iterdir()
    if path.is_file() and os.access(path, os.X_OK)
}
before = inventory()
python_before = {
    d.metadata["Name"].lower(): d.version for d in importlib.metadata.distributions()
}
selected = []
for item in lock["packages"]:
    expected_before = (
        [item["installed_version"], "installed"]
        if item["installed_version"] is not None
        else None
    )
    if before.get(item["package"]) != expected_before:
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
# Debian moved these commands between packages and from sbin to bin.
# Retain their existing absolute paths using explicit aliases to fixed payloads.
for name, target in lock["compatibility_aliases"].items():
    alias = Path(name)
    if name not in before_commands or alias.exists() or alias.is_symlink():
        raise SystemExit(f"Unexpected compatibility path: {name}")
    if not (alias.parent / target).is_file():
        raise SystemExit(f"Missing fixed command target: {target}")
    alias.symlink_to(target)
missing_commands = sorted(
    name
    for name in before_commands
    if not Path(name).is_file() or not os.access(name, os.X_OK)
)
if missing_commands:
    raise SystemExit(f"Original executable paths removed: {missing_commands}")
after = inventory()
expected_after = dict(before)
for item in lock["packages"]:
    expected_after[item["package"]] = [item["version"], "installed"]
if after != expected_after:
    raise SystemExit("Unexpected package set, versions or package state")
python_after = {
    d.metadata["Name"].lower(): d.version for d in importlib.metadata.distributions()
}
if python_after != python_before:
    raise SystemExit("Python distribution inventory changed")
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
            "no_other_distribution_changes": True,
            "original_executable_paths_preserved": len(before_commands),
            "added_packages": [
                x["package"] for x in lock["packages"] if x["installed_version"] is None
            ],
        }
    )
)
