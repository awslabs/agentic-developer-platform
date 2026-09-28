"""Apply the upstream security source hunks to cryptography 46.0.7."""

import hashlib
import json
from pathlib import Path
import subprocess

root = Path("/src/cryptography")
records = []
for name in ("crypto-chain-budget", "crypto-pkcs7"):
    patch = Path("/tmp") / (name + ".patch")
    chunks = patch.read_text().split("diff --git ")[1:]
    selected = "".join(
        "diff --git " + chunk for chunk in chunks if chunk.startswith("a/src/")
    )
    assert selected
    result = subprocess.run(
        ["patch", "--batch", "--fuzz=0", "-p1"],
        input=selected,
        text=True,
        cwd=root,
        capture_output=True,
    )
    print(result.stdout)
    if result.returncode:
        raise RuntimeError(result.stderr)
    records.append(
        {
            "patch": name,
            "upstream_patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
            "applied_source_hunks_sha256": hashlib.sha256(
                selected.encode()
            ).hexdigest(),
        }
    )
about = root / "src/cryptography/__about__.py"
s = about.read_text()
assert '__version__ = "46.0.7"' in s
about.write_text(s.replace('__version__ = "46.0.7"', '__version__ = "46.0.7+adp1"'))
project = root / "pyproject.toml"
s = project.read_text()
assert s.count('version = "46.0.7"') == 1
project.write_text(s.replace('version = "46.0.7"', 'version = "46.0.7+adp1"'))
files = [
    "src/rust/cryptography-x509-verification/src/lib.rs",
    "src/rust/cryptography-x509-verification/src/policy/mod.rs",
    "src/rust/src/pkcs7.rs",
]
Path("/crypto-source-manifest.json").write_text(
    json.dumps(
        {
            "upstream": "46.0.7",
            "local_version": "46.0.7+adp1",
            "patches": records,
            "source_sha256": {
                f: hashlib.sha256((root / f).read_bytes()).hexdigest() for f in files
            },
        },
        indent=2,
    )
    + "\n"
)
