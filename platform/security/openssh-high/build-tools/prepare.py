"""Check the archive-authenticated Debian source and add the reviewed backport."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

tools = Path(__file__).parent
lock = json.loads((tools / "source-lock.json").read_text())
for name, expected in lock["source_sha256"].items():
    actual = hashlib.sha256(Path(name).read_bytes()).hexdigest()
    if actual != expected:
        raise SystemExit(f"Source hash mismatch: {name}")
patch = tools / "CVE-2026-60002.patch"
if hashlib.sha256(patch.read_bytes()).hexdigest() != lock["backport_patch_sha256"]:
    raise SystemExit("Backport patch hash mismatch")
# apt-get source has already authenticated Sources through the pinned image's
# Debian archive keyring. Check the archive-pinned hashes above as well. This
# does not require trusting a separately downloaded uploader key.
subprocess.run(
    ["dpkg-source", "--no-check", "-x", "openssh_10.0p1-7+deb13u4.dsc", "/src/openssh"],
    check=True,
)
source = Path("/src/openssh")
subprocess.run(["git", "apply", "--check", str(patch)], cwd=source, check=True)
shutil.copyfile(patch, source / "debian/patches/CVE-2026-60002-adp.patch")
with (source / "debian/patches/series").open("a") as series:
    series.write("\nCVE-2026-60002-adp.patch\n")
subprocess.run(["dpkg-source", "--before-build", str(source)], check=True)
for advisory in ["CVE-2026-59999", "CVE-2026-60000"]:
    extra_patch = tools / (advisory + ".patch")
    assert (
        hashlib.sha256(extra_patch.read_bytes()).hexdigest()
        == lock["high_patch_sha256"][advisory]
    )
    subprocess.run(
        ["patch", "--dry-run", "--fuzz=0", "-p1", "-i", str(extra_patch)],
        cwd=source,
        check=True,
    )
    shutil.copyfile(extra_patch, source / "debian/patches" / extra_patch.name)
    with (source / "debian/patches/series").open("a") as series:
        series.write(extra_patch.name + "\n")
    subprocess.run(["dpkg-source", "--before-build", str(source)], check=True)

changelog = source / "debian/changelog"
changelog.write_text(
    f"openssh ({lock['downstream_version']}) trixie; urgency=high\n\n"
    "  * Backport upstream client connection-state lifetime repair for\n"
    "    CVE-2026-60002, CVE-2026-59999 and CVE-2026-60000; retain Debian choices.\n\n"
    " -- Security Maintenance <security-maintenance@users.noreply.github.com>"
    "  Sat, 26 Sep 2026 00:00:00 +0000\n\n" + changelog.read_text()
)
shutil.copyfile(tools / "NOTICE", source / "debian/ADP-security-NOTICE")
with (source / "debian/openssh-client.docs").open("a") as docs:
    docs.write("\ndebian/ADP-security-NOTICE\n")
