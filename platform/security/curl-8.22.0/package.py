"""Package genuine upstream binaries with Debian-compatible names and ABI."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

out = Path("/out")
rows = []
for package, flavor, lib in [
    ("curl", "openssl", None),
    ("libcurl4t64", "openssl", "libcurl.so.4"),
    ("libcurl3t64-gnutls", "gnutls", "libcurl-gnutls.so.4"),
]:
    root = Path("/packages") / package
    root.mkdir(parents=True)
    if lib:
        source = next(
            Path("/stage-" + flavor + "/usr/lib/x86_64-linux-gnu").glob(
                "libcurl.so.4.*"
            )
        )
        dest = root / "usr/lib/x86_64-linux-gnu" / (lib + ".0.0")
        dest.parent.mkdir(parents=True)
        shutil.copyfile(source, dest)
        if flavor == "gnutls":
            subprocess.run(["patchelf", "--set-soname", lib, str(dest)], check=True)
        dest.chmod(0o644)
        (dest.parent / lib).symlink_to(dest.name)
    else:
        dest = root / "usr/bin/curl"
        dest.parent.mkdir(parents=True)
        shutil.copyfile("/stage-openssl/usr/bin/curl", dest)
        dest.chmod(0o755)
    doc = root / "usr/share/doc" / package
    doc.mkdir(parents=True)
    shutil.copyfile("/src/COPYING", doc / "copyright")
    shutil.copyfile("/provenance/source-lock.json", doc / "adp-source-lock.json")
    control = subprocess.check_output(["dpkg-query", "-s", package], text=True)
    fields = {}
    key = None
    for line in control.splitlines():
        if line.startswith(" "):
            fields[key] += "\n" + line
        elif ": " in line:
            key, value = line.split(": ", 1)
            fields[key] = value
    for key in [
        "Status",
        "Config-Version",
        "Conffiles",
        "Triggers-Pending",
        "Triggers-Awaited",
        "Installed-Size",
    ]:
        fields.pop(key, None)
    fields.update(
        Version="8.22.0-0+adp1",
        Source="curl",
        Maintainer="ADP Security <security@example.invalid>",
    )
    # Versioned virtual provides describe the replacement itself. Keep Debian
    # transition Breaks bounds, but never advertise the old upstream version.
    if "Provides" in fields:
        fields["Provides"] = re.sub(
            r"\(= [^)]+\)", "(= 8.22.0-0+adp1)", fields["Provides"]
        )
    fields["Description"] = fields["Description"].replace("RTMP, ", "")
    if package == "curl":
        fields["Depends"] = re.sub(
            r"libcurl4t64 \(= [^)]+\)",
            "libcurl4t64 (= 8.22.0-0+adp1)",
            fields["Depends"],
        )
    (root / "DEBIAN").mkdir()
    if lib:
        (root / "DEBIAN/triggers").write_text("activate-noawait ldconfig\n")
    (root / "DEBIAN/control").write_text(
        "".join(k + ": " + v + "\n" for k, v in fields.items())
    )
    subprocess.run(
        [
            "dpkg-deb",
            "--root-owner-group",
            "--build",
            str(root),
            str(out / (package + "_8.22.0-0+adp1_amd64.deb")),
        ],
        check=True,
    )
    rows.append(
        {
            "package": package,
            "binary": str(dest.relative_to(root)),
            "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
        }
    )
(out / "binary-receipt.json").write_text(json.dumps(rows, indent=2) + "\n")
