"""Credential-free repository check entry point, executed only in a denied-network Pod."""

import base64
import json
import os
import sys
from hashlib import sha256
from pathlib import Path

from l5_services.adapters.transform.ndjson import source_path
from l5_services.adapters.transform.provider_runner import MAX_PAYLOAD_BYTES, clean_environment


def main():
    raw = b"".join(path.read_bytes() for path in sorted(Path("/input").glob("part-*")))
    if not raw or len(raw) > MAX_PAYLOAD_BYTES:
        raise ValueError("Transform check input exceeds its bound")
    if sys.argv[1:] != ["--input-sha256=" + sha256(raw).hexdigest()]:
        raise ValueError("Transform check mounted input differs from the admitted payload")
    payload = json.loads(raw)
    policy = json.loads(Path("/opt/transform-check-policy.json").read_text())
    if any(payload[key] != policy[key] for key in ("bundle_sha256", "lock_sha256", "oracle_version")):
        raise ValueError("Transform check image does not contain the selected test policy")
    command = payload["command"]
    if command not in policy["commands"]:
        raise ValueError("Transform check command is not installed in the pinned image")
    root, home = Path("/work/source"), Path("/work/home")
    root.mkdir(mode=0o700)
    home.mkdir(mode=0o700)
    (home / "tmp").mkdir()
    for name, content in payload["files"].items():
        path = root / source_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(content, validate=True))
        mode = payload["file_modes"][name]
        if mode not in {"100644", "100755"}:
            raise ValueError("Transform check file mode is invalid")
        path.chmod(0o755 if mode == "100755" else 0o644)
    cwd = root if command["cwd"] == "." else root / source_path(command["cwd"])
    environment = {**clean_environment(home), **command["environment"], "AWS_EC2_METADATA_DISABLED": "true"}
    if any(key.upper().startswith(("AWS_", "ADP_", "KUBERNETES_")) for key in command["environment"]):
        raise ValueError("Transform checks cannot receive provider or platform identity")
    os.chdir(cwd)
    os.execvpe(command["argv"][0], command["argv"], environment)


if __name__ == "__main__":
    main()
