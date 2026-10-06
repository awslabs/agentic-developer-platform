"""Replace only the pinned PyJWT wheel and its requirements metadata, offline."""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys

root = Path("/maintenance")
lock = json.loads((root / "artifact-lock.json").read_text())
requirements = Path("/requirements-security.txt").read_text()
expected = "PyJWT==" + lock["version"]
assert re.findall(r"^PyJWT==[^\s]+$", requirements, re.MULTILINE) == [expected]
assert importlib.metadata.version("PyJWT") == "2.13.0"
assert importlib.metadata.version("skypilot") == "0.12.3"
wheel = root / "artifacts" / lock["filename"]
assert wheel.stat().st_size == lock["size"]
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == lock["sha256"]
before = {
    d.metadata["Name"].lower(): d.version for d in importlib.metadata.distributions()
}
subprocess.run(
    [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-index",
        "--no-deps",
        "--no-cache-dir",
        "--no-compile",
        str(wheel),
    ],
    check=True,
    env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
)
after = {
    d.metadata["Name"].lower(): d.version for d in importlib.metadata.distributions()
}
assert before.keys() == after.keys()
assert {k for k in before if before[k] != after[k]} == {"pyjwt"}
assert after["pyjwt"] == lock["version"]
installed_requirements = Path("/opt/adp-security/requirements-security.txt")
text = installed_requirements.read_text()
assert re.findall(r"^PyJWT==[^\s]+$", text, re.MULTILINE) == ["PyJWT==2.13.0"]
installed_requirements.write_text(text.replace("PyJWT==2.13.0", expected))
subprocess.run(
    [sys.executable, "-m", "pip", "check"],
    check=True,
    env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
)
print(
    json.dumps(
        {
            "updated": "PyJWT",
            "from": "2.13.0",
            "to": lock["version"],
            "other_distribution_versions_unchanged": True,
        }
    )
)
