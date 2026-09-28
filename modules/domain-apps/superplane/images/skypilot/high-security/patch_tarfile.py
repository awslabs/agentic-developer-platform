"""Apply the reviewed hardlink fix to the exact SkyPilot Python 3.10 source."""

import hashlib
import json
import tarfile
from pathlib import Path

path = Path(tarfile.__file__)
before = path.read_bytes()
assert (
    hashlib.sha256(before).hexdigest()
    == "a5422cf74bbaacd6892b88b23629304cebd4a1a328f3287603133c8ca67d3f6d"
)
old = "os.link(tarinfo._link_target, targetpath)"
assert before.decode().count(old) == 1
after = (
    before.decode()
    .replace(old, "os.link(os.path.realpath(tarinfo._link_target), targetpath)")
    .encode()
)
path.write_bytes(after)
for cached in (path.parent / "__pycache__").glob("tarfile.*.pyc"):
    cached.unlink()
Path("/opt/adp-security/high-dependencies/tarfile.json").write_text(
    json.dumps(
        {
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "upstream_reference": "https://github.com/python/cpython/commit/b38be2e6cf9d989075ab73412c63e003ebad4ff3",
            "backport": "same hardlink resolution fix adapted to Python 3.10.21",
        },
        indent=2,
    )
    + "\n"
)
