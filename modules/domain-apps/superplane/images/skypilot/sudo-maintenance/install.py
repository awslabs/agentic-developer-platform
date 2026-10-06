"""Install only the authenticated sudo backport, retaining runtime policy."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess

root = Path("/maintenance")
lock = json.loads((root / "artifact-lock.json").read_text())
artifact = root / "artifacts" / lock["filename"]
if (
    artifact.stat().st_size != lock["size"]
    or hashlib.sha256(artifact.read_bytes()).hexdigest() != lock["sha256"]
):
    raise SystemExit("Sudo package checksum mismatch")
metadata = subprocess.check_output(
    ["dpkg-deb", "-f", str(artifact), "Package", "Version", "Architecture"], text=True
)
if (
    metadata
    != f"Package: sudo\nVersion: {lock['version']}\nArchitecture: {lock['architecture']}\n"
):
    raise SystemExit("Sudo package metadata mismatch")


def inventory():
    output = subprocess.check_output(
        ["dpkg-query", "-W", "-f=${Package}\t${Version}\t${db:Status-Status}\n"],
        text=True,
    )
    return {line.split("\t")[0]: line.split("\t")[1:] for line in output.splitlines()}


def policy():
    result = {}
    for path in Path("/etc").rglob("*"):
        if str(path).startswith(("/etc/sudo", "/etc/pam.d/sudo")):
            if path.is_symlink():
                result[str(path)] = {"target": os.readlink(path)}
            elif path.is_file():
                result[str(path)] = {
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "mode": path.stat().st_mode & 0o7777,
                }
    return result


before = inventory()
if before.get("sudo") != [lock["from_version"], "installed"]:
    raise SystemExit("Unexpected starting sudo package")
policy_before = policy()
python_before = {
    d.metadata["Name"].lower(): d.version for d in importlib.metadata.distributions()
}
subprocess.run(
    ["dpkg", "--force-confold", "-i", str(artifact)],
    check=True,
    env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"},
)
expected = dict(before)
expected["sudo"] = [lock["version"], "installed"]
if inventory() != expected:
    raise SystemExit("Unplanned package or state changes")
if policy() != policy_before:
    raise SystemExit("Sudo policy/configuration changed")
if {
    d.metadata["Name"].lower(): d.version for d in importlib.metadata.distributions()
} != python_before:
    raise SystemExit("Python distribution inventory changed")
if subprocess.check_output(["dpkg", "--audit"], text=True).strip():
    raise SystemExit("dpkg audit failed")
subprocess.run(["/usr/sbin/visudo", "-c"], check=True)
print(
    json.dumps(
        {
            "sudo": lock["version"],
            "policy_preserved": policy_before,
            "only_sudo_package_version_changed": True,
        }
    )
)
