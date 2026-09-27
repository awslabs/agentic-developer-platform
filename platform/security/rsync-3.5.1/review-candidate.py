"""Bind rsync vendor fixes to the actual candidate package, binary and source."""

import collections
import hashlib
import json
import subprocess
import sys
from pathlib import Path

source = Path(__file__).resolve().parent
proof = source.parents[2] / "docs/security/runs/2026-09-27/rsync-upstream"
scan = Path(sys.argv[1])
receipt = json.loads((scan / "receipt.json").read_text())
assert (
    hashlib.sha256((scan / "grype.json").read_bytes()).hexdigest()
    == receipt["grype_sha256"]
)
image = receipt["image"]
assert (
    subprocess.check_output(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
    ).strip()
    == receipt["docker_root_descriptor"]
)
script = """import hashlib,json,subprocess
from pathlib import Path
print(json.dumps({'version':subprocess.check_output(['dpkg-query','-W','-f=${Version}','rsync'],text=True),'binary_sha256':hashlib.sha256(Path('/usr/bin/rsync').read_bytes()).hexdigest(),'source_lock':json.loads(Path('/opt/adp-rsync-security/source-lock.json').read_text()),'runtime_version':subprocess.check_output(['rsync','--version'],text=True)}))"""
obs = json.loads(
    subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            "python",
            image,
            "-c",
            script,
        ],
        text=True,
    )
)
assert obs["source_lock"] == json.loads((source / "source-lock.json").read_text())
assert obs["version"] == "3.5.1-0+adp1"
assert (
    obs["binary_sha256"]
    == json.loads((source / "binary-receipt.json").read_text())["sha256"]
)
assert (
    "version 3.5.1" in obs["runtime_version"]
    and "protocol version 33" in obs["runtime_version"]
)
advisories = {
    a["cve_id"]: a for a in json.loads((proof / "advisories.json").read_text())
}
raw = json.loads((scan / "grype.json").read_text())
dispositions = []
for index, match in enumerate(raw["matches"]):
    v, a = match["vulnerability"], match["artifact"]
    if a["name"] != "rsync" or v["severity"] not in ["Critical", "High"]:
        continue
    assert a["version"] == obs["version"]
    advisory = advisories[v["id"]]
    assert all(
        p["patched_versions"] in ["3.5.0", "3.4.3", ">= 3.4.3"]
        for p in advisory["vulnerabilities"]
    )
    dispositions.append(
        {
            "native_match_index": index,
            "id": v["id"],
            "severity": v["severity"],
            "package": "rsync",
            "version": a["version"],
            "binary_sha256": obs["binary_sha256"],
            "status": "fixed-in-candidate",
            "vendor_url": advisory["html_url"],
            "ghsa_id": advisory["ghsa_id"],
            "vendor_fixed_ranges": advisory["vulnerabilities"],
        }
    )
assert len(dispositions) == 24
assert collections.Counter(d["severity"] for d in dispositions) == {
    "Critical": 5,
    "High": 19,
}
curl = json.loads((proof / "curl-review.json").read_text())
assert curl["docker_root_descriptor"] == receipt["docker_root_descriptor"]
indices = {d["native_match_index"] for d in dispositions + curl["dispositions"]}
remaining = collections.Counter(
    m["vulnerability"]["severity"]
    for i, m in enumerate(raw["matches"])
    if i not in indices and m["vulnerability"]["severity"] in ["Critical", "High"]
)
report = {
    "scope": "exact candidate only; not live closure",
    "raw_scan_receipt": receipt,
    "binary_observation": obs,
    "rsync_dispositions": dispositions,
    "curl_disposition_file": "curl-review.json",
    "reviewed_remaining_native_match_counts": dict(remaining),
    "remaining_openssh_criticals": "open; source client/server package applicability and fix remain separate",
}
Path(sys.argv[2]).write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"rsync_fixed": len(dispositions), "remaining": dict(remaining)}))
