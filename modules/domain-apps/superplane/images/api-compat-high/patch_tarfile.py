"""Apply CPython's exact 3.12 CVE-2026-82049 hunk to reviewed source bytes."""

import hashlib
import json
import tarfile
from pathlib import Path

manifest = json.loads((Path(__file__).parent / "tarfile-manifest.json").read_text())
path = Path(tarfile.__file__)
before = path.read_bytes()
assert hashlib.sha256(before).hexdigest() == manifest["before"]
old = "                    os.link(tarinfo._link_target, targetpath)"
new = """                    # Resolve the target so the hard link points to the file
                    # itself. Otherwise os.link() may duplicate a symlink to a
                    # shallower location, where its relative target escapes the
                    # destination directory. (CVE-2026-82049)
                    os.link(os.path.realpath(tarinfo._link_target), targetpath)"""
assert before.decode().count(old) == 1
after = before.decode().replace(old, new).encode()
assert hashlib.sha256(after).hexdigest() == manifest["after"]
path.write_bytes(after)
for cached in (path.parent / "__pycache__").glob("tarfile.*.pyc"):
    cached.unlink()
