"""Build locally from the exact locked OCI platform; do not publish anything."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("oci_layout", type=Path)
parser.add_argument("--tag", required=True)
parser.add_argument("--metadata-file", type=Path, required=True)
args = parser.parse_args()
root = Path(__file__).resolve().parent
lock = json.loads((root / "artifact-lock.json").read_text())
layout = args.oci_layout.resolve()
manifest = layout / "blobs" / "sha256" / lock["base_platform"].split(":")[1]
raw = manifest.read_bytes()
if "sha256:" + hashlib.sha256(raw).hexdigest() != lock["base_platform"]:
    raise SystemExit("Base platform digest mismatch")
if json.loads(raw)["config"]["digest"] != lock["base_config"]:
    raise SystemExit("Base configuration digest mismatch")
subprocess.run(
    [
        "docker",
        "buildx",
        "build",
        "--load",
        "--platform",
        "linux/amd64",
        "--network=none",
        "--progress=plain",
        "--build-context",
        f"sudo-base=oci-layout://{layout}@{lock['base_platform']}",
        "--build-arg",
        "BASE_IMAGE=sudo-base",
        "--metadata-file",
        str(args.metadata_file.resolve()),
        "-f",
        str(root / "Dockerfile"),
        "-t",
        args.tag,
        str(root),
    ],
    check=True,
)
