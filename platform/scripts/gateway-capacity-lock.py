#!/usr/bin/env python3
"""Own one target's gateway capacity changes until release or verified recovery."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

TABLE = "adp-terraform-locks"


def aws(region, *arguments):
    result = subprocess.run(["aws", "--region", region, *arguments],
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f"Gateway capacity ownership {arguments[1]} refused; no unfenced fallback")
    return json.loads(result.stdout) if result.stdout.strip() else {}


def target(account, region, environment, cluster, namespace):
    if not re.fullmatch(r"[0-9]{12}", account) or not all(
        re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", value)
        for value in (region, environment, cluster, namespace)
    ):
        raise ValueError("Invalid gateway ownership target")
    return f"adp-gateway-capacity/{account}/{region}/{environment}/{cluster}/{namespace}"


def lock_key(key):
    return {"LockID": {"S": key}}


def acquire(region, key, path):
    if path.exists():
        raise ValueError("Gateway ownership file already exists")
    owner = str(uuid.uuid4())
    started = str(int(time.time()))
    record = {"target": key, "region": region, "owner": owner, "started": started}
    aws(region, "dynamodb", "put-item", "--table-name", TABLE,
        "--item", json.dumps(dict(lock_key(key), Owner={"S": owner}, Started={"N": started})),
        "--condition-expression", "attribute_not_exists(LockID)")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(record, output)
    except Exception:
        release(region, record)
        raise
    return record


def current(region, key):
    return aws(region, "dynamodb", "get-item", "--table-name", TABLE,
               "--key", json.dumps(lock_key(key)), "--consistent-read").get("Item", {})


def matches(item, record):
    return (item.get("LockID") == {"S": record["target"]}
            and item.get("Owner") == {"S": record["owner"]}
            and item.get("Started") == {"N": record["started"]})


def verify(region, record):
    if not matches(current(region, record["target"]), record):
        raise RuntimeError("Gateway capacity ownership changed; refusing target mutation")


def release(region, record):
    aws(region, "dynamodb", "delete-item", "--table-name", TABLE,
        "--key", json.dumps(lock_key(record["target"])),
        "--condition-expression", "#owner = :owner AND #started = :started",
        "--expression-attribute-names", json.dumps({"#owner": "Owner", "#started": "Started"}),
        "--expression-attribute-values", json.dumps({":owner": {"S": record["owner"]},
                                                       ":started": {"N": record["started"]}}))


def profile_parameter(region, environment):
    name = f"/adp/{environment}/gateway/capacity-profile"
    result = subprocess.run(["aws", "--region", region, "ssm", "get-parameter", "--name", name,
                             "--output", "json"], text=True, capture_output=True, check=False)
    if result.returncode:
        if "ParameterNotFound" in result.stderr:
            return None, 0
        raise RuntimeError("Cannot read capacity selection; no unfenced fallback")
    parameter = json.loads(result.stdout)["Parameter"]
    return parameter["Value"], parameter["Version"]


def select(region, environment, key, path, profile, expected_version):
    if profile not in ("default", "llm-proxy") or expected_version is None or expected_version < 0:
        raise ValueError("Selection needs a supported profile and observed SSM version")
    record = acquire(region, key, path)
    attempted = False
    try:
        _, version = profile_parameter(region, environment)
        if version != expected_version:
            raise ValueError("Capacity selection version changed; refusing stale rollback or selection")
        attempted = True
        aws(region, "ssm", "put-parameter", "--name", f"/adp/{environment}/gateway/capacity-profile",
            "--type", "String", "--value", profile, "--overwrite")
        selected, new_version = profile_parameter(region, environment)
        if selected != profile or new_version != version + 1:
            raise RuntimeError("Capacity selection readback differs; owner retained for reconciliation")
    except Exception:
        if not attempted:
            release(region, record)
            path.unlink()
        raise
    release(region, record)
    path.unlink()


def recover(region, key, receipt):
    record = {name: receipt[name] for name in ("target", "owner", "started")}
    if record["target"] != key or receipt.get("stopped") is not True:
        raise ValueError("Recovery requires evidence that the exact previous owner stopped")
    expected = receipt.get("resources")
    if not isinstance(expected, dict) or set(expected) != {"deployment", "hpa", "pdb"}:
        raise ValueError("Recovery requires exact Deployment, HPA and PDB ownership bindings")
    for kind, name in (("deployment", "bedrockgateway"), ("hpa", "bedrockgateway"),
                       ("pdb", "bedrockgateway-pdb")):
        observed = subprocess.run(
            ["kubectl", "--request-timeout=30s", "-n", key.rsplit("/", 1)[-1],
             "get", f"{kind}/{name}", "--ignore-not-found", "-o", "json"],
            text=True, capture_output=True, check=False,
        )
        if observed.returncode:
            raise RuntimeError("Cannot verify recovery target Kubernetes resources")
        metadata = json.loads(observed.stdout)["metadata"] if observed.stdout.strip() else None
        binding = ({"uid": metadata.get("uid"), "resourceVersion": metadata.get("resourceVersion")}
                   if metadata else None)
        if binding != expected[kind]:
            raise RuntimeError("Capacity bundle changed since the stopped-owner receipt; refuse stale recovery")
    verify(region, record)
    release(region, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("acquire", "verify", "release", "recover", "select"))
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--owner-file", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--profile", choices=("default", "llm-proxy"))
    parser.add_argument("--expected-version", type=int)
    arguments = parser.parse_args()
    try:
        key = target(arguments.account, arguments.region, arguments.environment,
                     arguments.cluster, arguments.namespace)
        if arguments.action == "recover":
            if not arguments.receipt:
                raise ValueError("Exact stopped-owner receipt required for recovery")
            recover(arguments.region, key, json.loads(arguments.receipt.read_text()))
        else:
            if not arguments.owner_file:
                raise ValueError("Private owner file required")
            if arguments.action == "acquire":
                acquire(arguments.region, key, arguments.owner_file)
            elif arguments.action == "select":
                select(arguments.region, arguments.environment, key, arguments.owner_file,
                       arguments.profile, arguments.expected_version)
            else:
                record = json.loads(arguments.owner_file.read_text())
                if record.get("region") != arguments.region or record.get("target") != key:
                    raise ValueError("Ownership target differs from requested target")
                if arguments.action == "verify":
                    verify(arguments.region, record)
                else:
                    release(arguments.region, record)
                    arguments.owner_file.unlink()
    except (ValueError, RuntimeError, OSError, KeyError, json.JSONDecodeError) as error:
        parser.exit(1, f"Gateway capacity ownership failed: {error}\n")


if __name__ == "__main__":
    main()
