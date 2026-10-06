"""Stage independently reviewed local Debian artifacts; no downloads or builds."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--package-directory", action="append", required=True, type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parent
lock = json.loads((root / "artifact-lock.json").read_text())
output = root / "artifacts"
output.mkdir(exist_ok=True)
for package in lock["packages"]:
    candidates = [directory / package["file"] for directory in args.package_directory]
    matches = [
        path
        for path in candidates
        if path.is_file()
        and hashlib.sha256(path.read_bytes()).hexdigest() == package["sha256"]
    ]
    if not matches:
        raise SystemExit(f"Missing exact reviewed artifact: {package['file']}")
    destination = output / package["file"]
    if (
        destination.exists()
        and hashlib.sha256(destination.read_bytes()).hexdigest() != package["sha256"]
    ):
        raise SystemExit(f"Refusing to replace different artifact: {package['file']}")
    shutil.copyfile(matches[0], destination)
print(json.dumps({"staged_packages": len(lock["packages"]), "build_started": False}))
