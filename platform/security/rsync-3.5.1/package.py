"""Keep Debian integration files while replacing the full installed upstream payload."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

root = Path("/package")
# make install includes the upstream executable, helpers and manuals. Retain
# Debian service/configuration and maintainer scripts from its signed archive.
shutil.copytree("/stage", root, dirs_exist_ok=True)
control = root / "DEBIAN/control"
lines = control.read_text().splitlines()
lines = [
    line
    for line in lines
    if not line.startswith(("Installed-Size:", "Version:", "Maintainer:"))
]
lines += [
    "Version: 3.5.1-0+adp1",
    "Maintainer: ADP Security <security@example.invalid>",
]
lines = [
    line + ", libidn2-0 (>= 2.0.0)"
    if line.startswith("Depends:") and "libidn2-0" not in line
    else line
    for line in lines
]
control.write_text("\n".join(lines) + "\n")
shutil.copyfile(
    "/provenance/source-lock.json", root / "usr/share/doc/rsync/adp-source-lock.json"
)
md5 = []
for file in sorted(root.rglob("*")):
    if (
        file.is_file()
        and not file.is_symlink()
        and "DEBIAN" not in file.relative_to(root).parts
    ):
        md5.append(
            # Debian md5sums is compatibility metadata; SHA-256 below binds security evidence.
            hashlib.md5(file.read_bytes(), usedforsecurity=False).hexdigest()
            + "  "
            + str(file.relative_to(root))
        )
(root / "DEBIAN/md5sums").write_text("\n".join(md5) + "\n")
subprocess.run(
    [
        "dpkg-deb",
        "--root-owner-group",
        "--build",
        str(root),
        "/out/rsync_3.5.1-0+adp1_amd64.deb",
    ],
    check=True,
)
Path("/out/binary-receipt.json").write_text(
    json.dumps(
        {
            "binary": "/usr/bin/rsync",
            "sha256": hashlib.sha256((root / "usr/bin/rsync").read_bytes()).hexdigest(),
            "package": "rsync",
            "version": "3.5.1-0+adp1",
        },
        indent=2,
    )
    + "\n"
)
