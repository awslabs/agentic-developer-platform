"""Bind the authenticated SSH backport to one immutable scanned candidate.

Retains raw scanner findings. This is candidate evidence, never live closure.
"""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("scan_directory", type=Path)
parser.add_argument("--output", required=True, type=Path)
args = parser.parse_args()
source = Path(__file__).resolve().parent
receipt = json.loads((args.scan_directory / "receipt.json").read_text())
for filename, key in [("grype.json", "grype_sha256"), ("syft.json", "sbom_sha256")]:
    assert hashlib.sha256((args.scan_directory / filename).read_bytes()).hexdigest() == receipt[key]
image = receipt["image"]
root = subprocess.check_output(
    ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
).strip()
assert root == receipt["docker_root_descriptor"]
sbom = json.loads((args.scan_directory / "syft.json").read_text())
assert sbom["source"]["metadata"]["imageID"] == receipt["config_digest"]
proof = json.loads((source / "validation-20260926.json").read_text())
script = """import hashlib,json,subprocess
from pathlib import Path
print(json.dumps({'sha256':hashlib.sha256(Path('/usr/bin/ssh').read_bytes()).hexdigest(),
 'version':subprocess.check_output(['dpkg-query','-W','-f=${Version}','openssh-client'],text=True),
 'source_lock':json.loads(Path('/opt/deepwiki-security/openssh-source-lock.json').read_text())}))
"""
observed = json.loads(subprocess.check_output([
    "docker", "run", "--rm", "--network", "none", "--read-only",
    "--entrypoint", "python", image, "-c", script,
], text=True))
assert observed["sha256"] == proof["ssh_compatibility"]["ssh_sha256"]
assert observed["version"] == "1:10.0p1-7+deb13u4+adp.security.1"
assert observed["source_lock"] == json.loads((source / "source-lock.json").read_text())
raw = json.loads((args.scan_directory / "grype.json").read_text())
rows = []
for index, match in enumerate(raw["matches"]):
    vuln, artifact = match["vulnerability"], match["artifact"]
    if vuln["id"] != "CVE-2026-60002" or artifact["name"] != "openssh-client":
        continue
    assert artifact["version"] == observed["version"]
    rows.append({"native_match_index": index, "id": vuln["id"],
                 "severity": vuln["severity"], "artifact_id": artifact["id"],
                 "package": artifact["name"], "version": artifact["version"],
                 "status": "fixed-in-candidate", "binary": "/usr/bin/ssh",
                 "binary_sha256": observed["sha256"]})
assert len(rows) == 1, "expected exactly one SSH advisory occurrence"
args.output.write_text(json.dumps({
    "scope": "exact candidate only; not live closure",
    "docker_root_descriptor": root, "config_digest": receipt["config_digest"],
    "raw_receipt": receipt, "binary_observation": observed,
    "source_validation_sha256": hashlib.sha256((source / "validation-20260926.json").read_bytes()).hexdigest(),
    "dispositions": rows,
}, indent=2) + "\n")
print(json.dumps({"candidate": image, "ssh_dispositions": len(rows)}))
