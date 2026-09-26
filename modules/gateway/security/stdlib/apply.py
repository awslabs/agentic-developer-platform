"""Apply reviewed CPython security backports only to exact expected source bytes."""

import argparse
import hashlib
import json
import subprocess
import sysconfig
from pathlib import Path


def verify(stdlib, manifest, stage):
    for name, hashes in manifest.items():
        actual = hashlib.sha256((stdlib / name).read_bytes()).hexdigest()
        if actual != hashes[stage]:
            raise RuntimeError(f"Unexpected CPython source ({stage}): {name}; review the security backports")


def apply_bundle(stdlib, bundle, *, verify_only=False):
    manifest = json.loads((bundle / "manifest.json").read_text())
    if not verify_only:
        verify(stdlib, manifest, "before")
        subprocess.run(
            ["patch", "--batch", "--fuzz=0", "-p2", "-d", str(stdlib), "-i", str(bundle / "cpython-3.13.15.patch")],
            check=True,
        )
    verify(stdlib, manifest, "after")
    # The runtime base may carry old bytecode even after copying patched sources.
    for name in manifest:
        path = stdlib / name
        for cached in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
            cached.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    apply_bundle(Path(sysconfig.get_path("stdlib")), Path(__file__).parent, verify_only=args.verify_only)
