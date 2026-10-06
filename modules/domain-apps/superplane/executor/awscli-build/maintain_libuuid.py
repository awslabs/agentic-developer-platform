"""Replace the AWS CLI copy with the exact reviewed runtime-base libuuid bytes."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile


def checked_payload(path, expected):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular library file: {path}")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError(f"Library bytes differ from the reviewed lock: {path}")
    if data[:6] != b"\x7fELF\x02\x01" or data[18:20] != b"\x3e\x00":
        raise ValueError("Expected a little-endian amd64 ELF64 library")
    return data


def maintain():
    lock = json.loads(Path(__file__).with_name("libuuid-lock.json").read_text())
    source, destination = Path(lock["source"]), Path(lock["destination"])
    package = subprocess.check_output(
        [
            "dpkg-query",
            "-W",
            "-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}",
            lock["package"],
        ],
        text=True,
    )
    expected = "\t".join(
        [lock["package"], lock["version"], lock["architecture"], "installed"]
    )
    if package != expected:
        raise ValueError("Runtime-base libuuid package differs from the reviewed lock")
    subprocess.run(
        [
            "dpkg",
            "--compare-versions",
            lock["version"],
            "ge",
            lock["minimum_vendor_version"],
        ],
        check=True,
    )
    owner = subprocess.check_output(
        ["dpkg-query", "-S", str(source)], text=True
    ).strip()
    if owner != f"{lock['package']}:{lock['architecture']}: {source}":
        raise ValueError(
            "Selected source is not owned by the runtime-base libuuid package"
        )
    replacement = checked_payload(source, lock["sha256"])
    checked_payload(destination, lock["original_sha256"])
    original_mode = stat.S_IMODE(destination.stat().st_mode)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=".adp-libuuid-", delete=False
    ) as temporary:
        temporary.write(replacement)
        staged = Path(temporary.name)
    try:
        staged.chmod(original_mode)
        os.replace(staged, destination)
    finally:
        staged.unlink(missing_ok=True)
    checked_payload(destination, lock["sha256"])
    receipt = {
        "schema": "adp-awscli-libuuid-maintenance/v1",
        "source": "Exact libuuid1 payload from the selected reviewed runtime base",
        "package": lock["package"],
        "version": lock["version"],
        "architecture": lock["architecture"],
        "source_path": str(source),
        "destination_path": str(destination),
        "sha256": lock["sha256"],
        "replaced_sha256": lock["original_sha256"],
        "elf_metadata": "Preserved byte-for-byte with the complete library payload",
    }
    Path("/opt/awscli-adp/libuuid-maintenance.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )
    print(json.dumps(receipt))


if __name__ == "__main__":
    maintain()
