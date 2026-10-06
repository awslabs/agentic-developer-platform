"""Bind upstream-fixed curl dispositions to a scanned candidate's actual binaries.

This produces a candidate-only overlay. It never edits native scanner results or
changes running-workload totals. Unknown versions, hashes and IDs fail closed.
"""

import argparse
import collections
import hashlib
import json
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("scan_directory", type=Path)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument(
    "--runtime",
    choices=["python", "node"],
    default="python",
    help="Interpreter already present in the candidate",
)
args = parser.parse_args()
source = Path(__file__).resolve().parent
repo = source.parents[2]
proof = repo / "docs/security/runs/2026-09-27/curl-upstream"
scan = args.scan_directory
receipt = json.loads((scan / "receipt.json").read_text())
assert (
    hashlib.sha256((scan / "grype.json").read_bytes()).hexdigest()
    == receipt["grype_sha256"]
)
assert (
    hashlib.sha256((scan / "syft.json").read_bytes()).hexdigest()
    == receipt["sbom_sha256"]
)
image = receipt["image"]
root = subprocess.check_output(
    ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
).strip()
assert root == receipt["docker_root_descriptor"]
expected = json.loads((source / "binary-receipt.json").read_text())
script = """import hashlib,json,subprocess
from pathlib import Path
rows=[]
for package,binary in [('curl','/usr/bin/curl'),('libcurl4t64','/usr/lib/x86_64-linux-gnu/libcurl.so.4.0.0'),('libcurl3t64-gnutls','/usr/lib/x86_64-linux-gnu/libcurl-gnutls.so.4.0.0')]:
 version=subprocess.check_output(['dpkg-query','-W','-f=${Version}',package],text=True)
 rows.append({'package':package,'version':version,'binary':binary.lstrip('/'),'sha256':hashlib.sha256(Path(binary).read_bytes()).hexdigest(),'source_lock':json.loads(Path('/opt/adp-curl-security',package,'adp-source-lock.json').read_text())})
print(json.dumps(rows))"""
if args.runtime == "node":
    script = r"""const fs=require('fs'),cp=require('child_process'),crypto=require('crypto');
const rows=[['curl','/usr/bin/curl'],['libcurl4t64','/usr/lib/x86_64-linux-gnu/libcurl.so.4.0.0'],['libcurl3t64-gnutls','/usr/lib/x86_64-linux-gnu/libcurl-gnutls.so.4.0.0']].map(([packageName,binary])=>({package:packageName,version:cp.execFileSync('dpkg-query',['-W','-f=${Version}',packageName],{encoding:'utf8'}),binary:binary.slice(1),sha256:crypto.createHash('sha256').update(fs.readFileSync(binary)).digest('hex'),source_lock:JSON.parse(fs.readFileSync('/opt/adp-curl-security/'+packageName+'/adp-source-lock.json','utf8'))}));
console.log(JSON.stringify(rows));"""
observed = json.loads(
    subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--entrypoint",
            args.runtime,
            image,
            "-e" if args.runtime == "node" else "-c",
            script,
        ],
        text=True,
    )
)
source_lock = json.loads((source / "source-lock.json").read_text())
for row in observed:
    assert row["version"] == source_lock["package_version"]
    assert row["source_lock"] == source_lock
    assert {k: row[k] for k in ["package", "binary", "sha256"]} in expected
assert len(observed) == len(expected) == 3
advisories = json.loads((proof / "advisories/receipts.json").read_text())
for advisory in advisories:
    page = proof / "advisories" / (advisory["id"] + ".html")
    assert hashlib.sha256(page.read_bytes()).hexdigest() == advisory["sha256"]
    assert (
        "8.21.0" in advisory["recommendation"] or "8.22.0" in advisory["recommendation"]
    )
by_id = {a["id"]: a for a in advisories}
by_package = {p["package"]: p for p in observed}
raw = json.loads((scan / "grype.json").read_text())
rows = []
remaining = []
for index, match in enumerate(raw["matches"]):
    vuln, package = match["vulnerability"], match["artifact"]
    if vuln["severity"] not in ["Critical", "High"]:
        continue
    if (
        vuln["id"] in by_id
        and package["name"] in by_package
        and package["version"] == source_lock["package_version"]
    ):
        rows.append(
            {
                "native_match_index": index,
                "id": vuln["id"],
                "severity": vuln["severity"],
                "artifact_id": package["id"],
                "package": package["name"],
                "version": package["version"],
                "status": "fixed-in-candidate",
                "binary": by_package[package["name"]]["binary"],
                "binary_sha256": by_package[package["name"]]["sha256"],
                "vendor_evidence": by_id[vuln["id"]],
            }
        )
    else:
        remaining.append(match)
assert {r["id"] for r in rows} == set(by_id), (
    "candidate does not contain every reviewed curl advisory"
)
assert len(rows) == 54, "expected eighteen scanner advisories on each of three packages"
report = {
    "scope": "exact candidate only; not a live closure",
    "docker_root_descriptor": root,
    "config_digest": receipt["config_digest"],
    "raw_receipt": receipt,
    "source_lock": source_lock,
    "binary_observations": observed,
    "dispositions": rows,
    "reviewed_remaining_native_match_counts": dict(
        collections.Counter(m["vulnerability"]["severity"] for m in remaining)
    ),
    "unreviewed_findings_remain_open": True,
}
args.output.write_text(json.dumps(report, indent=2) + "\n")
print(
    json.dumps(
        {
            "candidate": image,
            "curl_dispositions": len(rows),
            "remaining": report["reviewed_remaining_native_match_counts"],
        }
    )
)
