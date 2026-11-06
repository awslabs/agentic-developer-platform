"""Build maintained Pillow wheels with locked upstream security fixes."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


root = Path("/build-tools")
lock = json.loads((root / "source-lock.json").read_text())
for source in lock["sources"]:
    assert sha(Path("/inputs") / source["filename"]) == source["sha256"]
for patch in lock["patches"]:
    assert sha(root / "patches" / patch["filename"]) == patch["sha256"]
    if "applied_filename" in patch:
        assert (
            sha(root / "patches" / patch["applied_filename"]) == patch["applied_sha256"]
        )

out = Path("/out")
out.mkdir()
receipts = {}
for version, abi in (
    ("12.2.0", "cp310"),
    ("12.3.0", "cp310"),
    ("13.0.0", "cp311"),
    ("13.0.0", "cp313"),
):
    source = Path("/src") / (version + "-" + abi)
    source.mkdir(parents=True)
    subprocess.run(
        [
            "tar",
            "-xzf",
            f"/inputs/pillow-{version}.tar.gz",
            "--strip-components=1",
            "-C",
            str(source),
        ],
        check=True,
    )
    changed = [
        "src/libImaging/TiffDecode.c",
        "src/libImaging/JpegDecode.c",
        "src/PIL/EpsImagePlugin.py",
        *[
            "src/PIL/" + name
            for name in (
                "Image.py",
                "JpegImagePlugin.py",
                "DcxImagePlugin.py",
                "MicImagePlugin.py",
                "MpoImagePlugin.py",
                "PsdImagePlugin.py",
            )
        ],
    ]
    before = {name: sha(source / name) for name in changed}
    for patch in lock["patches"] if version.startswith("12.") else []:
        subprocess.run(
            [
                "git",
                "apply",
                "--include=src/*",
                str(
                    root / "patches" / patch.get("applied_filename", patch["filename"])
                ),
            ],
            cwd=source,
            check=True,
        )
    version_file = source / "src/PIL/_version.py"
    text = version_file.read_text()
    assert f'__version__ = "{version}"' in text
    version_file.write_text(text.replace(f'"{version}"', f'"{version}+adp1"'))
    # Retain the licenses for the same native libraries used by upstream wheels.
    with (source / "LICENSE").open("a") as license_file:
        license_file.write(
            "\n\n----\nBrotli 1.2.1\n" + Path("/brotli-fixed/LICENSE").read_text()
        )
        for license_path in sorted(
            Path("/upstream/wheels/dependency_licenses").glob("*")
        ):
            license_file.write(
                f"\n\n----\n\n{license_path.name}\n\n{license_path.read_text()}"
            )
    subprocess.run(
        [
            f"/opt/python/{abi}-{abi}/bin/python",
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--config-settings=raqm=enable",
            "--config-settings=raqm=vendor",
            "--config-settings=fribidi=vendor",
            "--config-settings=imagequant=disable",
            "--wheel-dir=/unrepaired",
            ".",
        ],
        cwd=source,
        check=True,
    )
    wheel = next(Path("/unrepaired").glob(f"pillow-{version}+adp1-{abi}-*.whl"))
    subprocess.run(
        [
            "auditwheel",
            "repair",
            "--plat",
            "manylinux_2_28_x86_64",
            "-w",
            str(out),
            str(wheel),
        ],
        check=True,
    )
    receipts[version + "-" + abi] = {
        "native_dependencies": {
            **json.loads(Path("/upstream/.github/dependencies.json").read_text()),
            "brotli": "1.2.1",
        },
        "before_sha256": before,
        "patched_sha256": {name: sha(source / name) for name in changed},
    }
(out / "patched-source.json").write_text(json.dumps(receipts, indent=2) + "\n")
shutil.copyfile(root / "source-lock.json", out / "source-lock.json")
(out / "pillow-wheel-lock.json").write_text(
    json.dumps(
        [
            {
                "name": "pillow",
                "version": wheel.name.split("-")[1],
                "filename": wheel.name,
                "sha256": sha(wheel),
            }
            for wheel in sorted(out.glob("*.whl"))
        ],
        indent=2,
    )
    + "\n"
)
