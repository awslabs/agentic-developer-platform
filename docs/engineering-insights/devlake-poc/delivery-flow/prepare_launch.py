"""Materialize private bindings in an inert draft; never register or authorize it."""

import argparse
import copy
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


FIELDS = {"org_id", "product_id", "installation_id", "connection_id", "target", "resource_id", "launch_manifest", "expires_at"}


def bind(proposal, config, *, now):
    if not isinstance(config, dict) or set(config) != FIELDS:
        raise ValueError("Private configuration must contain exactly the documented binding fields")
    for name in FIELDS - {"installation_id", "expires_at"}:
        value = config[name]
        if not isinstance(value, str) or not value.strip() or value.startswith(("unresolved", "example", "org-resolved")):
            raise ValueError("Missing actual private binding: " + name)
    if type(config["installation_id"]) is not int or config["installation_id"] <= 0:
        raise ValueError("installation_id must be a verified positive integer")
    if not re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", config["product_id"]):
        raise ValueError("product_id must be the registered Product UUID")
    if not isinstance(config["expires_at"], str):
        raise ValueError("expires_at must be an ISO timestamp")
    expiry = datetime.fromisoformat(config["expires_at"].replace("Z", "+00:00"))
    if expiry.tzinfo is None or not now + timedelta(hours=56) <= expiry <= now + timedelta(days=14):
        raise ValueError("Expiry must allow at least 56 hours and at most 14 days from materialization")
    result = copy.deepcopy(proposal)
    result["org_id"] = config["org_id"]
    result["product_id"] = config["product_id"]
    policy = result["proposed_execution_policy"]
    policy["org_id"] = config["org_id"]
    policy["environment_connection_ids"] = [config["connection_id"]]
    policy["expires_at"] = expiry.isoformat()
    for node in result["nodes"]:
        node["repository_connection"] = {"provider": "github", "installation_id": config["installation_id"]}
        for item in node.get("required_inputs", []):
            if item["source"].startswith("environment_connection:"):
                item["source"] = "environment_connection:" + config["connection_id"]
            elif item["name"] == "registered-target":
                item["source"] = "environment:" + config["target"]
        if node.get("operational_release_binding"):
            release = node["operational_release_binding"]
            release["target"] = config["target"]
            contract = release["delivery_contract"]
            contract["connection_id"] = config["connection_id"]
            contract["resource_id"] = config["resource_id"]
            contract["workflow_inputs"]["launch_manifest"] = config["launch_manifest"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    root = folder.parents[3]
    sys.path.insert(0, str(root / "modules/gateway"))
    from src.orchestration.proposal import LoopProposal, validate_proposal
    try:
        bound = bind(json.loads((folder / "proposal.authored.json").read_text()), json.loads(args.config.read_text()), now=datetime.now(UTC))
        violations = validate_proposal(LoopProposal.model_validate(bound))
        if violations:
            raise ValueError("Bound proposal failed the native validator")
        if args.output:
            target = args.output.resolve()
            if target.is_relative_to(root):
                raise ValueError("Private output must be outside the repository")
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as output:
                json.dump(bound, output, indent=2)
                output.write("\n")
    except (ValueError, OSError, TypeError, KeyError):
        # Do not echo private configuration, IDs, endpoints or paths in diagnostics.
        print("Private draft not produced: check binding fields, expiry, native schema and an unused output outside the repository.", file=sys.stderr)
        return 1
    print("Private draft valid and still inert. Server readiness must verify bindings, actor authority and launch receipts before live operations. Already accepted flows require a native amendment preserving their current plan and progress.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
