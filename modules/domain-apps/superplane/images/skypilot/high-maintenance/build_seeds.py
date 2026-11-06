"""Refresh virtualenv's locked pip seeds without changing its dependency solve.

Python 3.10+ uses official pip 26.2.2. The Python 3.9 seed keeps pip 26.0.1
with only its vendored certifi refreshed, identified as 26.0.1+adp1.
"""

import base64
import csv
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unpack(data):
    with zipfile.ZipFile(io.BytesIO(data)) as wheel:
        return {
            name: wheel.read(name)
            for name in wheel.namelist()
            if not name.endswith("/")
        }


def repack(files, name, old, new):
    old_info, new_info = f"{name}-{old}.dist-info", f"{name}-{new}.dist-info"
    files = {path.replace(old_info, new_info): data for path, data in files.items()}
    metadata = f"{new_info}/METADATA"
    assert f"Version: {old}\n".encode() in files[metadata]
    files[metadata] = files[metadata].replace(
        f"Version: {old}\n".encode(), f"Version: {new}\n".encode()
    )
    record = f"{new_info}/RECORD"
    files.pop(record)
    rows = io.StringIO()
    writer = csv.writer(rows, lineterminator="\n")
    for path, data in sorted(files.items()):
        digest = (
            base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        )
        writer.writerow([path, "sha256=" + digest, len(data)])
    writer.writerow([record, "", ""])
    files[record] = rows.getvalue().encode()
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as wheel:
        for path, data in sorted(files.items()):
            info = zipfile.ZipInfo(path, (2026, 10, 24, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            wheel.writestr(info, data)
    return result.getvalue()


def main(root=Path("/seed-inputs"), out=Path("/seed-out")):
    if not __debug__:
        raise RuntimeError("Seed maintenance requires assertions")
    lock = json.loads((root / "seed-source-lock.json").read_text())
    inputs = {}
    for item in lock:
        data = (root / item["filename"]).read_bytes()
        assert sha(data) == item["sha256"]
        inputs[item["name"]] = data
    virtualenv = unpack(inputs["virtualenv"])
    prefix = "virtualenv/seed/wheels/embed/"
    legacy_name = "pip-26.0.1-py3-none-any.whl"
    legacy_bytes = virtualenv.pop(prefix + legacy_name)
    assert (
        sha(legacy_bytes)
        == "bdb1b08f4274833d62c1aa29e20907365a2ceb950410df15fc9521bad440122b"
    )
    legacy = unpack(legacy_bytes)
    certifi = unpack(inputs["certifi"])
    for path, data in certifi.items():
        if path.startswith("certifi/") and path.count("/") == 1:
            assert "pip/_vendor/" + path in legacy
            if path == "certifi/core.py":
                # Preserve pip's upstream namespace rewriting for resource APIs.
                assert data.count(b'"certifi"') == 4
                data = data.replace(b'"certifi"', b'"pip._vendor.certifi"')
                assert data == legacy["pip/_vendor/" + path]
            legacy["pip/_vendor/" + path] = data
    license_data = certifi["certifi-2026.10.19.dist-info/licenses/LICENSE"]
    legacy["pip/_vendor/certifi/LICENSE"] = license_data
    legacy["pip-26.0.1.dist-info/licenses/src/pip/_vendor/certifi/LICENSE"] = (
        license_data
    )
    vendors = legacy["pip/_vendor/vendor.txt"].decode()
    assert (
        len(re.findall(r"^([ \t]*)certifi==[^\n]+$", vendors, flags=re.MULTILINE)) == 1
    )
    legacy["pip/_vendor/vendor.txt"] = re.sub(
        r"^([ \t]*)certifi==[^\n]+$",
        r"\g<1>certifi==2026.10.19",
        vendors,
        flags=re.MULTILINE,
    ).encode()
    assert b'__version__ = "26.0.1"' in legacy["pip/__init__.py"]
    legacy["pip/__init__.py"] = legacy["pip/__init__.py"].replace(
        b'"26.0.1"', b'"26.0.1+adp1"'
    )
    patched_legacy = repack(legacy, "pip", "26.0.1", "26.0.1+adp1")
    mapping = virtualenv[prefix + "__init__.py"].decode()
    replacements = [
        (legacy_name, "pip-26.0.1+adp1-py3-none-any.whl", legacy_bytes, patched_legacy),
        (
            "pip-26.2.1-py3-none-any.whl",
            "pip-26.2.2-py3-none-any.whl",
            virtualenv.pop(prefix + "pip-26.2.1-py3-none-any.whl"),
            inputs["pip"],
        ),
    ]
    for old, new, before, after in replacements:
        assert old in mapping and sha(before) in mapping
        mapping = mapping.replace(old, new).replace(sha(before), sha(after))
        virtualenv[prefix + new] = after
    virtualenv[prefix + "__init__.py"] = mapping.encode()
    version = virtualenv["virtualenv/version.py"]
    assert b"'21.7.13'" in version and b"(21, 7, 13)" in version
    virtualenv["virtualenv/version.py"] = version.replace(
        b"'21.7.13'", b"'21.7.13+adp1'"
    ).replace(b"(21, 7, 13)", b"(21, 7, 13, 'adp1')")
    result = repack(virtualenv, "virtualenv", "21.7.13", "21.7.13+adp1")
    out.mkdir()
    filename = "virtualenv-21.7.13+adp1-py3-none-any.whl"
    (out / filename).write_bytes(result)
    (out / "seed-wheel-lock.json").write_text(
        json.dumps(
            [
                {
                    "name": "virtualenv",
                    "version": "21.7.13+adp1",
                    "filename": filename,
                    "sha256": sha(result),
                }
            ],
            indent=2,
        )
        + "\n"
    )
    (out / "seed-source-lock.json").write_text(json.dumps(lock, indent=2) + "\n")
    (out / "seed-receipt.json").write_text(
        json.dumps(
            {
                "pip_legacy_version": "26.0.1+adp1",
                "pip_legacy_wheel_sha256": sha(patched_legacy),
                "pip_current_version": "26.2.2",
                "pip_current_wheel_sha256": sha(inputs["pip"]),
                "vendored_certifi": "2026.10.19",
                "virtualenv_dependencies_unchanged": True,
            },
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
