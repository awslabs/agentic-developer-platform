"""Verify the full Debian source backport against installed SSH package binaries."""

import collections
import hashlib
import json
import subprocess
import sys
from pathlib import Path

source = Path(__file__).resolve().parent
repo = source.parents[2]
scan = Path(sys.argv[1])
output = Path(sys.argv[2])
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
expected = json.loads((source / "binary-receipt.json").read_text())
code = """import hashlib,json,pwd,subprocess,sys
from pathlib import Path
expected=json.loads(sys.argv[1]);observed=[]
for package in expected:
 version=subprocess.check_output(['dpkg-query','-W','-f=${Version}',package['package']],text=True)
 observed.append({'package':package['package'],'version':version,'binaries':[{'path':b['path'],'sha256':hashlib.sha256(Path(b['path']).read_bytes()).hexdigest()} for b in package['binaries']]})
print(json.dumps({'packages':observed,'uid1000':pwd.getpwuid(1000).pw_name,'source_lock':json.loads(Path('/opt/adp-openssh-security/source-lock.json').read_text())}))"""
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
            code,
            json.dumps(expected),
        ],
        text=True,
    )
)
assert obs["uid1000"] == "sky"
assert obs["source_lock"] == json.loads(
    (
        repo / "modules/agent-context/images/deepwiki/security/openssh/source-lock.json"
    ).read_text()
)
for row in obs["packages"]:
    match = next(p for p in expected if p["package"] == row["package"])
    assert row == {k: v for k, v in match.items() if k != "archive_sha256"}
raw = json.loads((scan / "grype.json").read_text())
rows = []
for index, m in enumerate(raw["matches"]):
    a, v = m["artifact"], m["vulnerability"]
    if v["id"] == "CVE-2026-60002" and a["name"] in {p["package"] for p in expected}:
        assert a["version"] == obs["source_lock"]["downstream_version"]
        rows.append(
            {
                "native_match_index": index,
                "id": v["id"],
                "severity": v["severity"],
                "package": a["name"],
                "version": a["version"],
                "status": "fixed-source-package-in-candidate",
                "basis": "complete authenticated Debian source package rebuilt with the reviewed upstream client-lifetime patch; client, server and SFTP package identities retained",
                "actual_binaries": next(
                    p["binaries"] for p in obs["packages"] if p["package"] == a["name"]
                ),
            }
        )
assert len(rows) == 3 and all(r["severity"] == "Critical" for r in rows)
curl = json.loads((output.parent / "curl-review.json").read_text())
rsync = json.loads((output.parent / "rsync-review.json").read_text())
assert (
    curl["docker_root_descriptor"]
    == rsync["raw_scan_receipt"]["docker_root_descriptor"]
    == receipt["docker_root_descriptor"]
)
indices = {
    r["native_match_index"]
    for r in rows + curl["dispositions"] + rsync["rsync_dispositions"]
}
remaining = collections.Counter(
    m["vulnerability"]["severity"]
    for i, m in enumerate(raw["matches"])
    if i not in indices and m["vulnerability"]["severity"] in ["Critical", "High"]
)
report = {
    "scope": "exact candidate only; not live closure",
    "raw_scan_receipt": receipt,
    "binary_observations": obs,
    "openssh_dispositions": rows,
    "inherited_reviews": ["curl-review.json", "rsync-review.json"],
    "reviewed_remaining_native_match_counts": dict(remaining),
    "other_openssh_advisories": "remain open; the patch addresses CVE-2026-60002 only",
}
output.write_text(json.dumps(report, indent=2) + "\n")
print(
    json.dumps({"source_backport_occurrences": len(rows), "remaining": dict(remaining)})
)
