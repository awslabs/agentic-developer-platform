"""Bind CVE-2026-6653's upstream fix to an exact rebuilt ABI-2 library."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("scan", type=Path)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
receipt = json.loads((args.scan / "receipt.json").read_text())
raw_bytes = (args.scan / "grype.json").read_bytes()
assert hashlib.sha256(raw_bytes).hexdigest() == receipt["grype_sha256"]
assert (
    subprocess.check_output(
        ["docker", "image", "inspect", receipt["image"], "--format", "{{.Id}}"], text=True
    ).strip()
    == receipt["docker_root_descriptor"]
)
code = """import ctypes,hashlib,json,subprocess
from pathlib import Path
path=Path('/usr/lib/x86_64-linux-gnu/libxml2.so.2')
lib=ctypes.CDLL(str(path))
print(json.dumps({'version':subprocess.check_output(['dpkg-query','-W','-f=${Version}','libxml2'],text=True),'runtime_version':ctypes.c_char_p.in_dll(lib,'xmlParserVersion').value.decode(),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))"""
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
            "python",
            receipt["image"],
            "-c",
            code,
        ],
        text=True,
    )
)
assert observed == {
    "version": "2.13.9-0+adp1",
    "runtime_version": "21309",
    "sha256": "b38d226e6b1126549ea9c3e7a903b23c0be88a42cc5eb16f807811197f3fb337",
}
raw = json.loads(raw_bytes)
dispositions = []
for index, match in enumerate(raw["matches"]):
    if match["artifact"]["name"] != "libxml2" or match["vulnerability"]["id"] != "CVE-2026-6653":
        continue
    assert match["artifact"]["version"] == observed["version"]
    dispositions.append(
        {
            "native_match_index": index,
            "id": "CVE-2026-6653",
            "package": "libxml2",
            "version": observed["version"],
            "status": "fixed-upstream-source-in-exact-image",
            "binary_sha256": observed["sha256"],
            "fix_commit": "463bbeeca1805b5c4828f50d0fefc4eebaf620df",
            "upstream_release": "2.13.9",
            "basis": "Debian identifies upstream 2.11.0 as fixed; its 2.14.5 package threshold reflects the Debian source-version reversion, not this upstream source build.",
        }
    )
assert len(dispositions) <= 1
report = {
    "config_digest": receipt["config_digest"],
    "raw_receipt": receipt,
    "binary_observation": observed,
    "dispositions": dispositions,
    "scope": "exact candidate only; not live closure",
}
args.output.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps({"exact_xml_dispositions": len(dispositions), "binary_verified": True}))
