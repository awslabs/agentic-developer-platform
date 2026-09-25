"""Apply reviewed CPython security backports only to exact expected source bytes."""
import hashlib
import json
from pathlib import Path
import subprocess
import sysconfig

bundle = Path(__file__).parent
stdlib = Path(sysconfig.get_path("stdlib"))
manifest = json.loads((bundle / "manifest.json").read_text())

def verify(stage):
    for name, hashes in manifest.items():
        actual = hashlib.sha256((stdlib / name).read_bytes()).hexdigest()
        if actual != hashes[stage]:
            raise RuntimeError(f"Unexpected CPython source ({stage}): {name}; review the security backports")

verify("before")
subprocess.run(["patch", "--batch", "--fuzz=0", "-p2", "-d", str(stdlib), "-i", str(bundle / "cpython-3.14.7.patch")], check=True)
verify("after")
# Remove previously compiled bytecode so the patched source is always loaded.
for name in manifest:
    path = stdlib / name
    for cached in (path.parent / "__pycache__").glob(path.stem + ".*.pyc"):
        cached.unlink()
