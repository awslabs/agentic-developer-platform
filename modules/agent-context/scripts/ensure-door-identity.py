#!/usr/bin/env python3
"""Copy the gateway's public verification keyring; never read private signing keys."""
import argparse
import hashlib
import json
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", required=True)
    args = parser.parse_args()
    source = json.loads(subprocess.check_output([
        "kubectl", "get", "configmap", "adp-control-verification-keys",
        "-n", "adp-agents", "-o", "json",
    ]))
    keys = source["data"]["keys.json"]
    if not isinstance(json.loads(keys), dict) or not json.loads(keys):
        raise SystemExit("Gateway public verification keyring is empty; prepare agent authority first")
    target = {"apiVersion": "v1", "kind": "ConfigMap",
              "metadata": {"name": "agent-context-door-identity", "namespace": args.namespace},
              "data": {"keys.json": keys}}
    subprocess.run(["kubectl", "apply", "-f", "-"], input=json.dumps(target), text=True, check=True, stdout=subprocess.DEVNULL)
    print(hashlib.sha256(keys.encode()).hexdigest())


if __name__ == "__main__":
    main()
