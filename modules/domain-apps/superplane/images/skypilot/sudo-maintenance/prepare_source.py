"""Stage authenticated Debian source and the narrow upstream sudo backport."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source-artifacts", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parent
lock = json.loads((root / "source-lock.json").read_text())
output = root / "artifacts"
output.mkdir(exist_ok=True)
source = output / "source"
if source.exists():
    raise SystemExit("Source output already exists; use a clean preparation directory")
for item in lock["artifacts"]:
    if args.source_artifacts:
        data = (args.source_artifacts / item["file"]).read_bytes()
    else:
        with urllib.request.urlopen(item["url"], timeout=60) as response:
            data = response.read()
    if len(data) != item["size"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise SystemExit(f"Source artifact mismatch: {item['file']}")
    (output / item["file"]).write_bytes(data)
# The reviewed lock authenticates every source input through the signed archive
# index. Do not claim a direct descriptor signature that has not been checked.
subprocess.run(
    [
        "dpkg-source",
        "--no-check",
        "-x",
        str(output / "sudo_1.9.16p2-3+deb13u2.dsc"),
        str(source),
    ],
    check=True,
)
patch = root / "adp-fixed-working-timezone.patch"
if hashlib.sha256(patch.read_bytes()).hexdigest() != lock["patch"]["patch_sha256"]:
    raise SystemExit("Backport patch checksum mismatch")
installed = source / "src/sudo.c"
if hashlib.sha256(installed.read_bytes()).hexdigest() != lock["patch"]["before_sha256"]:
    raise SystemExit("Unexpected Debian sudo source")
shutil.copy2(patch, source / "debian/patches" / patch.name)
series = source / "debian/patches/series"
series.write_text(series.read_text() + "\n" + patch.name + "\n")
subprocess.run(["dpkg-source", "--before-build", str(source)], check=True)
if hashlib.sha256(installed.read_bytes()).hexdigest() != lock["patch"]["after_sha256"]:
    raise SystemExit("Unexpected patched sudo source")
changelog = source / "debian/changelog"
changelog.write_text(
    f"sudo ({lock['package_version']}) trixie; urgency=high\n\n"
    "  * Backport upstream working-environment timezone isolation (CVE-2026-96512).\n\n"
    " -- ADP Security Maintenance <security@adp.invalid>  Tue, 06 Oct 2026 06:50:00 +0000\n\n"
    + changelog.read_text()
)
print(
    json.dumps(
        {
            "source": str(source),
            "package_version": lock["package_version"],
            "patched_source_sha256": lock["patch"]["after_sha256"],
        }
    )
)
