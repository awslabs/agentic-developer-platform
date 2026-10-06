"""Verify wheel integrity and unchanged Python runtime source before installation."""

import base64
import csv
import hashlib
import io
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

DIST = "cryptography-46.0.7+adp1.dist-info"


def verify_wheel(wheel, site):
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("duplicate wheel members")
        record = DIST + "/RECORD"
        rows = list(csv.reader(io.StringIO(archive.read(record).decode())))
        records = {row[0]: row for row in rows if len(row) == 3}
        if len(records) != len(rows) or set(records) != set(names):
            raise ValueError("wheel RECORD coverage mismatch")
        verified = {}
        source = {}
        for item in archive.infolist():
            name = item.filename
            path = PurePosixPath(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or str(path) != name
                or len(path.parts) < 2
                or path.parts[0] not in {"cryptography", DIST}
                or item.is_dir()
                or stat.S_ISLNK(item.external_attr >> 16)
                or path.suffix == ".pyc"
            ):
                raise ValueError(f"unexpected wheel payload: {name}")
            data = archive.read(name)
            sha = hashlib.sha256(data).digest()
            expected = "sha256=" + base64.urlsafe_b64encode(sha).decode().rstrip("=")
            if name == record:
                if records[name][1:] != ["", ""]:
                    raise ValueError("invalid self RECORD row")
            elif records[name][1:] != [expected, str(len(data))]:
                raise ValueError("wheel payload hash or size mismatch")
            verified[name] = sha.hex()
            if path.parts[0] == "cryptography" and path.suffix in {".py", ".pyi"}:
                prior = site / name
                if not prior.is_file() or prior.read_bytes() != data:
                    raise ValueError(f"runtime source differs: {name}")
                source[name] = sha.hex()
        old_source = {
            str(p.relative_to(site))
            for p in (site / "cryptography").rglob("*")
            if p.suffix in {".py", ".pyi"}
        }
        if set(source) != old_source:
            raise ValueError("runtime source file set differs")
        native = [n for n in names if n.endswith(".so")]
        if native != ["cryptography/hazmat/bindings/_rust.abi3.so"]:
            raise ValueError("unexpected native extension set")
        return {"files": verified, "unchanged_runtime_source_files": len(source)}


if __name__ == "__main__":
    import json

    print(json.dumps(verify_wheel(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
