"""Approved ordinary Pod probe image entrypoint; no Kubernetes credentials."""

import json
import sys
from pathlib import Path

from .network_probe import probe


def run(contract):
    endpoint = f"http://{contract['service_name']}.{contract['namespace']}.svc.cluster.local:{contract['port']}/"
    result = {
        "version": 1,
        "nonce": contract["nonce"],
        "source": "pod",
        **probe(endpoint, allowed_cidrs=contract["cidrs"]),
    }
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    termination = json.dumps({"superplane_result_version": 1, "text": encoded})
    if len(termination.encode()) >= 4096 or len(result["addresses"]) > 32:
        raise ValueError("probe receipt exceeds result bound")
    return encoded, termination


def main():
    try:
        if len(sys.argv) != 2 or len(sys.argv[1]) > 1024:
            raise ValueError("invalid invocation")
        encoded, termination = run(json.loads(sys.argv[1]))
        # Kubernetes mounts this file for the workload container. An unavailable
        # termination channel cannot be replaced with a claimed successful result.
        Path("/dev/termination-log").write_text(termination)
        print(encoded, flush=True)
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit(
            "ordinary Pod network probe failed; no success result"
        ) from None


if __name__ == "__main__":
    main()
