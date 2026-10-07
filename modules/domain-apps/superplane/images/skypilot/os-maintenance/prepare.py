"""Stage the exact retained Debian artifacts; never download replacements."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("artifact_directory", type=Path)
args = parser.parse_args()
root = Path(__file__).resolve().parent
lock = json.loads((root / "artifact-lock.json").read_text())
target = root / "artifacts"
target.mkdir(exist_ok=True)
for item in lock["packages"]:
    source = args.artifact_directory / item["file"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit(f"Checksum mismatch: {source.name}")
    shutil.copy2(source, target / source.name)
print(f"Staged {len(lock['packages'])} authenticated artifacts")
