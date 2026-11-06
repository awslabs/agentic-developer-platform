"""Require AMI-baked, digest-addressable non-root images before job admission."""

import json
import re
import subprocess
import sys


def verify(references, run=subprocess.run):
    if len(references) != 2 or len(set(references)) != 2:
        raise ValueError("invalid image manifest")
    for reference in references:
        if re.fullmatch(r"[a-z0-9./_-]+(?::[a-zA-Z0-9._-]+)?@sha256:[0-9a-f]{64}", reference) is None:
            raise ValueError("invalid image reference")
        result = run(["docker", "image", "inspect", reference], capture_output=True, check=False)
        if result.returncode != 0:
            raise ValueError("image absent from local Docker store")
        images = json.loads(result.stdout)
        repository, digest = reference.split("@sha256:")
        canonical = repository.split(":", 1)[0] + "@sha256:" + digest
        if (len(images) != 1 or canonical not in images[0].get("RepoDigests", []) or
                images[0].get("Os") != "linux" or images[0].get("Architecture") != "amd64" or
                images[0].get("Config", {}).get("User") != "10001:10001"):
            raise ValueError("unverified local image")


if __name__ == "__main__":
    try:
        verify(sys.argv[1:])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, OSError):
        sys.exit(1)
