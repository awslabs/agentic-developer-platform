"""Replace one audited distribution without uninstalling files of other owners."""

import csv
import hashlib
import json
import subprocess
import sys
from pathlib import Path

SITE = Path("/usr/local/lib/python3.10/site-packages")
DIST = "cryptography-46.0.7+adp1.dist-info"
RECORD = SITE / DIST / "RECORD"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    wheel, inventory_path, receipt = map(Path, sys.argv[1:])
    inventory = json.loads(inventory_path.read_text())
    if digest(RECORD) != inventory["record_sha256"]:
        raise ValueError("old cryptography RECORD differs from reviewed inventory")
    from verify_wheel import verify_wheel

    wheel_report = verify_wheel(wheel, SITE)
    owners = {}
    audited = {r["path"]: r for r in inventory["ownership_audit"]["records"]}
    actual = {str(p): p for p in SITE.rglob("*.dist-info/RECORD")}
    if set(audited) != set(actual):
        raise ValueError("installed distribution set changed")
    for name, record in actual.items():
        if digest(record) != audited[name]["sha256"]:
            raise ValueError("installed ownership RECORD changed")
        for row in csv.reader(record.read_text().splitlines()):
            if len(row) != 3:
                raise ValueError("invalid installed RECORD row")
            path = (record.parent.parent / row[0]).resolve(strict=False)
            owners.setdefault(path, set()).add(record.parent.name)
    selected = set()
    removed = []
    preserved = []
    # Preflight every mutation before touching any file.
    for row in inventory["rows"]:
        path = Path(row["path"])
        if not path.is_relative_to(SITE) or path.resolve() != path:
            raise ValueError("inventory path is not a canonical site-package path")
        entry = row["installed_entry"]
        if (
            path.is_symlink()
            or not path.is_file()
            or entry["kind"] != "file"
            or digest(path) != entry["sha256"]
            or path.stat().st_size != entry["size"]
            or path.stat().st_mode & 0o7777 != entry["mode"]
            or owners.get(path, set()) != set(row["all_record_owners"])
        ):
            raise ValueError(f"installed file or ownership changed: {path}")
        selected.add(path)
        if owners[path] != {DIST}:
            if path.is_relative_to(SITE / "cryptography") or path.is_relative_to(
                SITE / DIST
            ):
                raise ValueError("shared runtime cryptography file")
            preserved.append(row)
        else:
            removed.append(row)
    for directory in [SITE / "cryptography", SITE / DIST]:
        for path in directory.rglob("*"):
            if (path.is_file() or path.is_symlink()) and path not in selected:
                raise ValueError(f"unrecorded runtime payload: {path}")
    for row in removed:
        Path(row["path"]).unlink()
    # No pip uninstall: shared generic docs/tests are deliberately untouched.
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--ignore-installed",
            "--no-cache-dir",
            str(wheel),
        ],
        check=True,
    )
    for row in preserved:
        if digest(Path(row["path"])) != row["installed_entry"]["sha256"]:
            raise ValueError("shared file changed")
    receipt.write_text(
        json.dumps(
            {
                "wheel_sha256": digest(wheel),
                "wheel_verification": wheel_report,
                "old_inventory_sha256": digest(inventory_path),
                "removed": removed,
                "preserved_shared": preserved,
                "installed_record_sha256": digest(RECORD),
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
