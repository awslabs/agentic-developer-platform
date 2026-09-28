"""Verify application/config preservation for a package-only worker overlay.

Usage: python3 verify-high-tools-overlay.py BASE_IMAGE CANDIDATE_IMAGE OUTPUT_DIR
Containers are read-only and offline. This does not deploy either image.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

base, candidate, output = sys.argv[1:]
images = [json.loads(subprocess.check_output(["docker", "inspect", ref]))[0]
          for ref in (base, candidate)]
base_layers = images[0]["RootFS"]["Layers"]
assert images[1]["RootFS"]["Layers"][:len(base_layers)] == base_layers
assert images[0]["Config"] == images[1]["Config"]
script = """import hashlib,json
from pathlib import Path
print(json.dumps({str(f):hashlib.sha256(f.read_bytes()).hexdigest()
 for f in sorted(Path('/app').rglob('*')) if f.is_file()},sort_keys=True))
"""
inventories = [subprocess.check_output([
    "docker", "run", "--rm", "--network", "none", "--read-only",
    "--entrypoint", "python3", ref, "-c", script,
], timeout=900) for ref in (base, candidate)]
assert inventories[0] == inventories[1]
result = {
    "base": base, "candidate": candidate,
    "docker_root_descriptor": images[1]["Id"],
    "base_layers_preserved": True, "configuration_identical": True,
    "app_files_identical": True,
    "app_file_count": len(json.loads(inventories[0])),
    "app_hash_inventory_sha256": hashlib.sha256(inventories[0]).hexdigest(),
}
directory = Path(output)
directory.mkdir(parents=True, exist_ok=True)
(directory / "worker-overlay-app-hashes.json").write_bytes(inventories[0])
(directory / "worker-overlay-preservation.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
