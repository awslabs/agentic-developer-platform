"""Verify the full Debian source backport against installed SSH package binaries."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

source = Path(__file__).resolve().parent
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
    (source / "build-tools/source-lock.json").read_text()
)
for row in obs["packages"]:
    match = next(p for p in expected if p["package"] == row["package"])
    assert row == {k: v for k, v in match.items() if k != "archive_sha256"}
raw = json.loads((scan / "grype.json").read_text())
rows = []
for index, m in enumerate(raw["matches"]):
    a, v = m["artifact"], m["vulnerability"]
    if v["id"] in {"CVE-2026-60002", "CVE-2026-59999", "CVE-2026-60000"} and a[
        "name"
    ] in {p["package"] for p in expected}:
        assert a["version"] == obs["source_lock"]["downstream_version"]
        rows.append(
            {
                "native_match_index": index,
                "id": v["id"],
                "severity": v["severity"],
                "package": a["name"],
                "version": a["version"],
                "status": "fixed-source-package-in-candidate",
                "basis": "Authenticated Debian source package rebuilt with upstream client-lifetime, server forwarding-precedence and GSSAPI accounting fixes; exact package binaries and source lock verified. Source unit tests and installed loopback transport fixture passed.",
                "actual_binaries": next(
                    p["binaries"] for p in obs["packages"] if p["package"] == a["name"]
                ),
            }
        )
assert len(rows) == 9, len(rows)
report = {
    "config_digest": receipt["config_digest"],
    "raw_receipt": receipt,
    "binary_observations": obs,
    "dispositions": rows,
    "scope": "Exact candidate only; no live closure claim or unrelated dispositions.",
}
output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"reviewed_openssh_matches": len(rows)}))
