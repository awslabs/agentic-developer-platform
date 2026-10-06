"""Offline replacement of only the reviewed Ray bundled Jackson jar."""

import base64
import hashlib
import importlib.metadata
import json
from pathlib import Path
import stat


def update_record(record_bytes, expected_row, replacement_bytes):
    text = record_bytes.decode("utf-8")
    rows = [
        row for row in text.splitlines() if row.startswith("ray/jars/ray_dist.jar,")
    ]
    if rows != [expected_row]:
        raise ValueError("original Ray RECORD differs from reviewed metadata")
    encoded = (
        base64.urlsafe_b64encode(hashlib.sha256(replacement_bytes).digest())
        .decode()
        .rstrip("=")
    )
    updated = (
        "ray/jars/ray_dist.jar,sha256=" + encoded + "," + str(len(replacement_bytes))
    )
    return text.replace(rows[0], updated, 1).encode("utf-8")


def main():
    root = Path("/maintenance")
    lock = json.loads((root / "artifact-lock.json").read_text())
    replacement = json.loads((root / "replacement-lock.json").read_text())
    if importlib.metadata.version("ray") != lock["ray_version"]:
        raise ValueError("unexpected Ray installation")
    jar = Path("/usr/local/lib/python3.10/site-packages/ray/jars/ray_dist.jar")
    if (
        not jar.is_file()
        or jar.is_symlink()
        or hashlib.sha256(jar.read_bytes()).hexdigest() != lock["original_jar_sha256"]
    ):
        raise ValueError("Ray jar is not the reviewed original")
    raw = (root / "artifacts/ray_dist.jar").read_bytes()
    if hashlib.sha256(raw).hexdigest() != replacement["sha256"]:
        raise ValueError("replacement differs from tested shaded artifact")
    record = jar.parents[2] / ("ray-" + lock["ray_version"] + ".dist-info/RECORD")
    updated_record = update_record(
        record.read_bytes(), lock["original_ray_record_row"], raw
    )
    mode = stat.S_IMODE(jar.stat().st_mode)
    jar.write_bytes(raw)
    jar.chmod(mode)
    record.write_bytes(updated_record)
    if hashlib.sha256(jar.read_bytes()).hexdigest() != replacement["sha256"]:
        raise ValueError("replacement write not verified")
    print(
        json.dumps(
            {
                "replaced": "ray_dist.jar",
                "jackson": lock["jackson_version"],
                "sha256": replacement["sha256"],
            }
        )
    )


if __name__ == "__main__":
    main()
