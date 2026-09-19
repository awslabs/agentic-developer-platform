#!/usr/bin/env python3
"""Transfer known legacy KMS ownership without changing any AWS resources."""
import argparse
import json
from pathlib import Path
import subprocess
import sys


def managed(state):
    result = {}
    for resource in state.get("resources", []):
        if resource.get("mode") != "managed":
            continue
        prefix = resource.get("module", "")
        address = (prefix + "." if prefix else "") + resource["type"] + "." + resource["name"]
        for instance in resource.get("instances", []):
            suffix = "[" + json.dumps(instance["index_key"]) + "]" if "index_key" in instance else ""
            result[address + suffix] = instance["attributes"]
    return result


def transfers(states, alias):
    source = managed(states.get("webhook-ingress", {}))
    platform = managed(states["platform"])
    gateway = managed(states.get("gateway", {}))
    result = []
    for old, new, expected in (
        ("aws_kms_key.secrets", "aws_kms_key.webhook_secrets", alias["TargetKeyId"]),
        ("aws_kms_alias.secrets", "aws_kms_alias.webhook_secrets", alias["AliasName"]),
    ):
        before, after = source.get(old), platform.get(new)
        if not before:
            continue
        field = "name" if "alias" in old else "id"
        if before.get(field) != expected or (after and after.get(field) != expected):
            raise ValueError("Conflicting KMS ownership for " + new + "; retain the unexpected key before migration")
        if "alias" in old and before.get("target_key_id") != alias["TargetKeyId"]:
            raise ValueError("Legacy webhook alias differs from its live target")
        if after and "alias" in old and after.get("target_key_id") != alias["TargetKeyId"]:
            raise ValueError("Platform webhook alias differs from its live target")
        result.append(("webhook-ingress", old, "platform", new, expected, not bool(after)))
    # Older webhook state also tracked the gateway's DynamoDB alias. Remove
    # only its duplicate tracking, and only when gateway owns that exact alias.
    before = source.get("aws_kms_alias.gateway_dynamodb")
    if before:
        after = gateway.get("aws_kms_alias.dynamodb")
        if not after or any(before.get(k) != after.get(k) for k in ("name", "target_key_id")):
            raise ValueError("Cannot establish gateway ownership of its shared DynamoDB alias")
        result.append(("webhook-ingress", "aws_kms_alias.gateway_dynamodb", "gateway",
                       "aws_kms_alias.dynamodb", before["name"], False))
    return result


def run(command, cwd=None):
    process = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if process.returncode:
        # Terraform errors can contain sensitive state; keep full output private.
        raise RuntimeError(f"{command[0]} {command[1]} failed: {process.stderr.strip()}")
    return process.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--directory", required=True)
    args = parser.parse_args()
    root, directory = Path(args.root), Path(args.directory)
    before = json.loads((directory / "integration-before.json").read_text())
    account, environment = before["account"], before["environment"]
    caller = json.loads(run(["aws", "sts", "get-caller-identity", "--output", "json"]))
    if caller["Account"] != account:
        raise ValueError("Migration account differs from upgrade account")
    states = {name: json.loads((directory / (name + "-before.tfstate")).read_text()) for name in before["modules"]}
    old = managed(states.get("webhook-ingress", {}))
    if not any(name in old for name in ("aws_kms_key.secrets", "aws_kms_alias.secrets", "aws_kms_alias.gateway_dynamodb")):
        print("No legacy KMS ownership migration required")
        return
    aliases = json.loads(run(["aws", "kms", "list-aliases", "--output", "json"]))["Aliases"]
    alias = next((a for a in aliases if a["AliasName"] == f"alias/adp-{environment}-webhook-secrets"), None)
    if not alias:
        raise ValueError("Legacy webhook encryption alias is missing")
    transfers(states, alias)  # Fail on conflicting ownership before mutations.
    paths = {"platform": root / "platform/infra", "gateway": root / "modules/gateway/infra",
             "webhook-ingress": root / "modules/agent-factory/webhook-ingress/infra"}
    for module in ("platform", "webhook-ingress", "gateway"):
        if module not in states:
            continue
        backend = root / "environments" / environment / ("backend.tfvars" if module == "platform" else "modules/" + module + "-backend.tfvars")
        run(["terraform", "init", "-input=false", "-reconfigure", "-backend-config=" + str(backend)], paths[module])
        raw = run(["terraform", "state", "pull"], paths[module])
        snapshot = directory / (module + "-before-migration.tfstate")
        snapshot.write_text(raw)
        snapshot.chmod(0o600)
        states[module] = json.loads(raw)
    actions = transfers(states, alias)
    for source, old_address, destination, new_address, identifier, needs_import in actions:
        if needs_import:
            variables = root / "environments" / environment / "platform.tfvars"
            run(["terraform", "import", "-input=false", "-var-file=" + str(variables),
                 "-var-file=" + str(directory / "platform.tfvars.json"), new_address, identifier], paths[destination])
        # Import first so a failed import leaves source ownership intact.
        destination_state = managed(json.loads(run(["terraform", "state", "pull"], paths[destination])))
        field = "name" if "aws_kms_alias." in new_address else "id"
        if destination_state.get(new_address, {}).get(field) != identifier:
            raise ValueError("Destination ownership verification failed for " + new_address)
        source_state = managed(json.loads(run(["terraform", "state", "pull"], paths[source])))
        if source_state.get(old_address, {}).get(field) != identifier:
            raise ValueError("Source ownership changed during migration")
        run(["terraform", "state", "rm", old_address], paths[source])
        print(f"Transferred state ownership: {source}/{old_address} -> {destination}/{new_address}", flush=True)
    current = json.loads(run(["aws", "kms", "describe-key", "--key-id", alias["AliasName"], "--output", "json"]))["KeyMetadata"]
    if current["KeyId"] != alias["TargetKeyId"] or current["KeyState"] != "Enabled":
        raise ValueError("Webhook KMS identity or availability changed during migration")
    (directory / "kms-migration.json").write_text(json.dumps({"key_id": current["KeyId"], "transfers": len(actions), "preserved": True}))
    print("Legacy KMS state ownership migrated; live key and alias unchanged")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, RuntimeError, OSError) as error:
        sys.exit(str(error))
