"""Verify the installed package against the pinned artifact before runtime proof."""

import hashlib
import zipfile
from importlib.metadata import version
from importlib.resources import files
from pathlib import Path


def release_identity(artifact_dir=Path("/app/l5-release")):
    wheels = list(artifact_dir.glob("adp_l5_services-*.whl"))
    if len(wheels) != 1:
        raise ValueError("one pinned L5 wheel is required")
    wheel = wheels[0]
    recorded = (artifact_dir / f"{wheel.name}.sha256").read_text().split()[0]
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if recorded != digest:
        raise ValueError("L5 wheel digest differs from the pinned manifest")
    packaged_schema = files("l5_services").joinpath("contracts/v1/schemas.json").read_bytes()
    with zipfile.ZipFile(wheel) as archive:
        if packaged_schema != archive.read("l5_services/contracts/v1/schemas.json"):
            raise ValueError("installed L5 contract differs from the pinned wheel")
    package_version = version("adp-l5-services")
    if not wheel.name.startswith(f"adp_l5_services-{package_version}-"):
        raise ValueError("installed L5 version differs from the pinned wheel")
    return {
        "l5_package_version": package_version,
        "l5_wheel_sha256": digest,
        "l5_schema_sha256": hashlib.sha256(packaged_schema).hexdigest(),
    }
